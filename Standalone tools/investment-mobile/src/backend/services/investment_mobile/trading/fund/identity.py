"""FundIdentityRegistry — fund body vs share class, collision-safe.

The same fund can have many share classes (currency / accumulation vs
distribution / hedged variants). They share ``fund_id`` but are distinct
tradable instruments — lookups by ``instrument_id`` include the class.
Chinese fund names are never used as identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import FundClassification, FundIdentity


class FundIdentityRegistry:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-identities.json"
        self._cls_path = Path(state_dir) / "fund-classifications.json"
        self._identities: dict[str, FundIdentity] = {}   # instrument_id → identity
        self._classes: dict[str, FundClassification] = {}  # fund_id → classification
        self._load()

    def _load(self) -> None:
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                i = FundIdentity(**row)
                self._identities[i.instrument_id] = i
        except Exception:
            pass
        try:
            rows = json.loads(self._cls_path.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                c = FundClassification(
                    fund_id=row["fund_id"], asset_class=row["asset_class"],
                    region=row["region"], sector=row.get("sector", ""),
                    theme=row.get("theme", ""),
                    source_id=row.get("source_id", "operator"),
                    source_version=row.get("source_version", ""),
                )
                self._classes[c.fund_id] = c
        except Exception:
            pass

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            [i.to_dict() for i in self._identities.values()],
            ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            ident = FundIdentity(
                fund_id=str(payload["fund_id"]),
                fund_name=str(payload["fund_name"]),
                fund_company=str(payload.get("fund_company") or ""),
                fund_type=str(payload.get("fund_type") or "other"),
                share_class_id=str(payload["share_class_id"]),
                isin=str(payload.get("isin") or ""),
                domicile=str(payload.get("domicile") or ""),
                base_currency=str(payload.get("base_currency") or ""),
                share_class_name=str(payload.get("share_class_name") or ""),
                share_class_currency=str(payload.get("share_class_currency") or ""),
                distribution_policy=str(
                    payload.get("distribution_policy") or "accumulation"),
                hedged_currency=str(payload.get("hedged_currency") or ""),
                inception_date=str(payload.get("inception_date") or ""),
                status=str(payload.get("status") or "active"),
            )
        except KeyError as exc:
            return {"ok": False, "error_code": "FIELD_MISSING",
                    "field": str(exc)}
        self._identities[ident.instrument_id] = ident
        self._persist()
        return {"ok": True, "identity": ident.to_dict()}

    def classify(self, payload: dict[str, Any]) -> dict[str, Any]:
        fund_id = str(payload.get("fund_id") or "")
        if fund_id not in {i.fund_id for i in self._identities.values()}:
            return {"ok": False, "error_code": "FUND_UNKNOWN"}
        c = FundClassification(
            fund_id=fund_id,
            asset_class=str(payload.get("asset_class") or "other"),
            region=str(payload.get("region") or "global"),
            sector=str(payload.get("sector") or ""),
            theme=str(payload.get("theme") or ""),
            source_id=str(payload.get("source_id") or "operator"),
            source_version=str(payload.get("source_version") or ""),
        )
        self._classes[fund_id] = c
        return {"ok": True, "classification": c.to_dict()}

    # ------------------------------------------------------------------
    def get(self, instrument_id: str) -> dict[str, Any] | None:
        i = self._identities.get(str(instrument_id))
        return i.to_dict() if i else None

    def share_classes(self, fund_id: str) -> list[dict[str, Any]]:
        return [
            i.to_dict() for i in self._identities.values()
            if i.fund_id == fund_id
        ]

    def list(self, fund_id: str | None = None) -> list[dict[str, Any]]:
        items = self._identities.values()
        if fund_id:
            items = [i for i in items if i.fund_id == fund_id]
        return [i.to_dict() for i in items]

    def classification(self, fund_id: str) -> dict[str, Any] | None:
        c = self._classes.get(str(fund_id))
        return c.to_dict() if c else None
