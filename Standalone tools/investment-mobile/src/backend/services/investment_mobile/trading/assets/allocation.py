"""AssetAllocationEngine — multi-dimension allocation analysis.

Dimensions: account, market, country, currency, asset_type, sector,
instrument, strategy. Targets are user-owned: set_target writes only
through the governed path; AI proposals are stored as suggestions and
never mutate targets.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

DIMENSIONS = ("account", "market", "country", "currency",
              "asset_type", "sector", "instrument", "strategy")


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class AssetAllocationEngine:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "allocation-targets.json"
        self._targets: dict[str, dict[str, Any]] = {}
        self._suggestions: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._targets = data.get("targets", {})
            self._suggestions = data.get("suggestions", [])
        except Exception:
            self._targets, self._suggestions = {}, []

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            {"targets": self._targets, "suggestions": self._suggestions},
            indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    def set_target(self, dimension: str, key: str, weight, *,
                   drift_band="0.05", max_weight=None,
                   actor: str = "user") -> dict[str, Any]:
        if dimension not in DIMENSIONS:
            return {"ok": False, "error_code": "DIMENSION_UNKNOWN",
                    "dimensions": list(DIMENSIONS)}
        if actor.lower() in ("ai", "xingcheng", "model", "assistant"):
            return {"ok": False, "error_code": "AI_CANNOT_SET_TARGET"}
        self._targets.setdefault(dimension, {})[key] = {
            "weight": str(_d(weight)),
            "drift_band": str(_d(drift_band)),
            "max_weight": str(_d(max_weight)) if max_weight else "",
            "set_by": actor, "set_at": time.time(),
        }
        self._persist()
        return {"ok": True}

    def propose(self, dimension: str, key: str, weight, *,
                rationale: str, actor: str = "ai") -> dict[str, Any]:
        """AI suggestions land here — stored, never applied."""
        self._suggestions.append({
            "dimension": dimension, "key": key,
            "weight": str(_d(weight)), "rationale": rationale,
            "actor": actor, "at": time.time(), "applied": False,
        })
        self._persist()
        return {"ok": True, "stored": "suggestion"}

    # ------------------------------------------------------------------
    def analyze(self, valuation: dict[str, Any],
                tags: dict[str, dict[str, str]] | None = None
                ) -> dict[str, Any]:
        """Actual weights per dimension vs targets + drift breach list."""
        total = _d(valuation.get("total_assets") or "0")
        if total <= 0:
            return {"ok": False, "error_code": "EMPTY_PORTFOLIO"}
        tags = tags or {}
        actual: dict[str, dict[str, Decimal]] = {
            d: {} for d in DIMENSIONS}
        for p in valuation.get("positions", []):
            v = _d(p.get("market_value_display")
                   or p.get("market_value"))
            if v <= 0:
                continue
            meta = tags.get(p["instrument_id"], {})
            dims = {
                "account": p["account_id"],
                "market": p.get("market") or "unknown",
                "country": meta.get("country", "unknown"),
                "currency": p.get("cost_currency", ""),
                "asset_type": meta.get("asset_type", "equity"),
                "sector": meta.get("sector", "unknown"),
                "instrument": p["instrument_id"],
                "strategy": meta.get("strategy", "manual"),
            }
            for dim, key in dims.items():
                actual[dim][key] = actual[dim].get(key, Decimal(0)) + v
        # cash contributes to currency/account dims
        for aid, bals in valuation.get("cash_detail", {}).items():
            for cur, b in bals.items():
                v = _d(b.get("balance"))
                actual["currency"][cur] = actual["currency"].get(
                    cur, Decimal(0)) + v
                actual["account"][aid] = actual["account"].get(
                    aid, Decimal(0)) + v
        weights = {
            d: {k: str(v / total) for k, v in rows.items()}
            for d, rows in actual.items()}
        breaches = []
        for dim, keys in self._targets.items():
            for key, t in keys.items():
                w = _d(weights.get(dim, {}).get(key, "0"))
                tw = _d(t["weight"])
                if abs(w - tw) > _d(t["drift_band"]):
                    breaches.append({
                        "dimension": dim, "key": key,
                        "actual": str(w), "target": str(tw),
                        "drift": str(w - tw)})
                mw = t.get("max_weight")
                if mw and w > _d(mw):
                    breaches.append({
                        "dimension": dim, "key": key,
                        "actual": str(w), "max_weight": mw,
                        "kind": "concentration_cap"})
        return {"ok": True, "total_assets": str(total),
                "weights": weights, "targets": self._targets,
                "breaches": breaches,
                "suggestions": list(self._suggestions)}
