"""PortfolioExposureEngine — cross-instrument look-through.

DirectExposure: shares held outright.
IndirectExposure: ETF/fund underlying constituents (with disclosure
date — stale baskets flagged).
EstimatedExposure: modelled overlap when constituents are missing —
incomplete data is NEVER read as "no overlap".

Look-through exposure is analytical only: indirect values are reported
separately and are NEVER added into portfolio market value (no double
counting).
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


def _s(v: Decimal) -> str:
    """Decimal → string without noise trailing zeros."""
    if v == v.to_integral_value():
        return str(v.quantize(Decimal(1)))
    return str(v.normalize())


class PortfolioExposureEngine:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "exposure-lookthrough.json"
        self._baskets: dict[str, dict[str, Any]] = {}
        self._related: dict[str, list[str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._baskets = data.get("baskets", {})
            self._related = data.get("related", {})
        except Exception:
            self._baskets, self._related = {}, {}

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            {"baskets": self._baskets, "related": self._related},
            indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    def register_basket(self, instrument_id: str,
                        constituents: list[dict[str, Any]], *,
                        disclosed_at: float,
                        source: str) -> dict[str, Any]:
        """ETF/fund holdings file: [{instrument_id|issuer, weight}]."""
        self._baskets[str(instrument_id)] = {
            "constituents": constituents,
            "disclosed_at": float(disclosed_at),
            "source": source,
        }
        self._persist()
        return {"ok": True, "instrument_id": instrument_id,
                "constituents": len(constituents)}

    def register_related(self, group_id: str,
                         instrument_ids: list[str]) -> dict[str, Any]:
        """Related-exposure groups (2330 ↔ TSM ADR ↔ semi ETFs…)."""
        self._related[str(group_id)] = [str(i) for i in instrument_ids]
        self._persist()
        return {"ok": True}

    # ------------------------------------------------------------------
    def analyze(self, valuation: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        direct: dict[str, Decimal] = {}
        indirect: dict[str, Decimal] = {}
        estimated: dict[str, Decimal] = {}
        basket_meta: dict[str, dict[str, Any]] = {}

        for p in valuation.get("positions", []):
            v = _d(p.get("market_value_display")
                   or p.get("market_value"))
            iid = p["instrument_id"]
            direct[iid] = direct.get(iid, Decimal(0)) + v
            basket = self._baskets.get(iid)
            if basket:
                stale = now - basket["disclosed_at"] > 92 * 86400
                basket_meta[iid] = {
                    "disclosed_at": basket["disclosed_at"],
                    "stale": stale, "source": basket["source"]}
                for c in basket["constituents"]:
                    key = str(c.get("issuer") or c.get("instrument_id"))
                    w = _d(c.get("weight"))
                    indirect[key] = indirect.get(key, Decimal(0)) \
                        + v * w
            # related-group estimated overlap (qualitative flag)
            for gid, members in self._related.items():
                if iid in members:
                    for other in members:
                        if other != iid:
                            estimated[gid] = estimated.get(
                                gid, Decimal(0)) + v
                            break

        total = _d(valuation.get("total_assets") or "0")
        issuer_view: dict[str, dict[str, str]] = {}
        for issuer, v in indirect.items():
            issuer_view[issuer] = {"indirect": _s(v)}
        return {
            "ok": True,
            "direct": {k: _s(v) for k, v in direct.items()},
            "indirect": issuer_view,
            "estimated_groups": {k: _s(v) for k, v in
                                 estimated.items()},
            "baskets": basket_meta,
            "total_assets": str(total),
            "note": "indirect/estimated exposures are analytical views "
                    "— never added into market value; missing baskets "
                    "are incomplete data, not zero overlap",
        }
