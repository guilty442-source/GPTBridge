"""Health view — one aggregated status surface (§24).

Aggregates lifecycle, model, market data, fund data, database, strategy
runtime, autotrade, jobs, budgets and metrics. Model availability does
NOT equal all-healthy — each subsystem reports independently.
"""
from __future__ import annotations

from typing import Any


class InvestmentHealthView:
    def __init__(self, *, probes: dict[str, Any]) -> None:
        # name -> callable returning {"ok"/status dict}
        self._probes = probes

    def health(self) -> dict[str, Any]:
        sub: dict[str, Any] = {}
        degraded: list[str] = []
        for name, probe in self._probes.items():
            try:
                r = probe() if callable(probe) else {"ok": bool(probe)}
            except Exception as exc:
                r = {"ok": False, "error_code": type(exc).__name__}
            sub[name] = r
            if not r.get("ok", True):
                degraded.append(name)
        return {
            "ok": True,
            "overall": "HEALTHY" if not degraded else "DEGRADED",
            "degraded": degraded,
            "subsystems": sub,
            "note": "模型可用 ≠ 全部投資服務正常——各自獨立回報",
        }
