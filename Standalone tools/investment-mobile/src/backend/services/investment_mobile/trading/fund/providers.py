"""FundDataProvider — replaceable fund data sources + controlled import.

Capability matrix states what each source legally provides and whether
it is verified. Bypassing logins, access controls or scraping protected
pages is prohibited. Without a verified official API, data enters via
controlled file import (CSV / JSON / XLSX) with validation, format
normalization and dedup against the NAV store.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .contracts import FundNAV, NavType


@dataclass
class FundProviderCapability:
    provider_id: str
    label: str
    public_data: bool
    api: bool
    automation_allowed: bool
    requires_auth: bool
    update_frequency: str          # daily | intraday | monthly | manual
    latency: str                   # e.g. "T+0 evening" | "15min"
    verified: bool = False         # official terms + access verified
    legal_basis: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "public_data": self.public_data,
            "api": self.api,
            "automation_allowed": self.automation_allowed,
            "requires_auth": self.requires_auth,
            "update_frequency": self.update_frequency,
            "latency": self.latency,
            "verified": self.verified,
            "legal_basis": self.legal_basis,
            "notes": self.notes,
        }


# Honest capability matrix — all real sources pending verification.
PROVIDER_CAPABILITIES: dict[str, FundProviderCapability] = {
    "tdcc": FundProviderCapability(
        provider_id="tdcc", label="臺灣集中保管結算所",
        public_data=True, api=False, automation_allowed=False,
        requires_auth=False, update_frequency="daily",
        latency="T+0 evening", legal_basis="public announcements",
        notes="境內基金淨值公告；自動化介面待驗證",
    ),
    "fund-clear": FundProviderCapability(
        provider_id="fund-clear", label="基金資訊觀測站/投信投顧公會",
        public_data=True, api=False, automation_allowed=False,
        requires_auth=False, update_frequency="daily",
        latency="T+0 evening", legal_basis="public disclosure",
        notes="境外基金總代理申報資料；正式 API 待確認",
    ),
    "fund-company": FundProviderCapability(
        provider_id="fund-company", label="各投信基金公司/境外總代理",
        public_data=True, api=False, automation_allowed=False,
        requires_auth=False, update_frequency="daily",
        latency="varies", legal_basis="company disclosure",
        notes="各公司格式不一；API 需逐家驗證",
    ),
    "prospectus": FundProviderCapability(
        provider_id="prospectus", label="公開說明書/月報/投資人須知",
        public_data=True, api=False, automation_allowed=False,
        requires_auth=False, update_frequency="monthly",
        latency="document", legal_basis="official filings",
        notes="費用/持股/策略權威來源；語意檢索走 Qdrant",
    ),
    "manual-import": FundProviderCapability(
        provider_id="manual-import", label="受控檔案匯入 (CSV/JSON/XLSX)",
        public_data=True, api=True, automation_allowed=True,
        requires_auth=False, update_frequency="manual",
        latency="operator", verified=True,
        legal_basis="operator-supplied data",
        notes="always available; operator responsible for provenance",
    ),
}

_REQUIRED_NAV_FIELDS = ("fund_id", "share_class_id", "nav_date", "nav")


class FundDataProvider:
    """Interface: fetch is not implemented until a source is verified."""

    capability: FundProviderCapability

    async def fetch_nav(self, *a: Any, **k: Any) -> list[FundNAV]:
        raise NotImplementedError(
            "no verified fund data API — use controlled file import"
        )


def _norm_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize column aliases to the FundNAV field names."""
    out = {str(k).strip().lower(): v for k, v in row.items()}
    alias = {
        "fund": "fund_id", "class": "share_class_id",
        "share_class": "share_class_id", "date": "nav_date",
        "value": "nav", "price": "nav", "ccy": "currency",
    }
    for src, dst in alias.items():
        if src in out and dst not in out:
            out[dst] = out.pop(src)
    return out


def import_nav_rows(
    rows: list[dict[str, Any]],
    source_id: str,
) -> tuple[list[FundNAV], list[dict[str, Any]]]:
    """Validate + normalize imported NAV rows. Returns (valid, rejected)."""
    valid: list[FundNAV] = []
    rejected: list[dict[str, Any]] = []
    for i, raw in enumerate(rows):
        row = _norm_row(raw)
        missing = [f for f in _REQUIRED_NAV_FIELDS if not row.get(f)]
        if missing:
            rejected.append({"row": i, "error": "missing_fields",
                             "fields": missing})
            continue
        try:
            nav = FundNAV(
                fund_id=str(row["fund_id"]).strip(),
                share_class_id=str(row["share_class_id"]).strip(),
                nav_date=row["nav_date"],
                nav=Decimal(str(row["nav"])),
                currency=str(row.get("currency") or "").upper(),
                source_id=source_id,
                nav_type=str(row.get("nav_type") or NavType.PUBLISHED.value),
                revision=int(row.get("revision") or 1),
            )
        except (InvalidOperation, ValueError) as exc:
            rejected.append({"row": i, "error": str(exc)})
            continue
        if nav.nav <= 0:
            rejected.append({"row": i, "error": "non_positive_nav"})
            continue
        valid.append(nav)
    return valid, rejected


def parse_nav_file(path: Path) -> list[dict[str, Any]]:
    """Parse CSV / JSON / XLSX into row dicts."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        text = path.read_text(encoding="utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("rows") or data.get("nav") or []
        return list(data)
    if suffix == ".xlsx":
        try:
            import openpyxl  # type: ignore
        except ImportError:
            raise ValueError("XLSX import requires openpyxl")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(c).strip() if c is not None else "" for c in rows[0]]
        return [
            {header[i]: v for i, v in enumerate(r) if i < len(header)}
            for r in rows[1:] if any(v is not None for v in r)
        ]
    raise ValueError(f"unsupported file type {suffix!r}")
