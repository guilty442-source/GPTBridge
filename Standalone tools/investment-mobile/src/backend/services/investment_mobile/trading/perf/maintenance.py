"""InvestmentMaintenanceService — tiered checks, not per-second scans.

Four check tiers (§15):
    incremental  — cheap counters, every event
    periodic     — moderate checks on a schedule
    event        — triggered by specific failures
    full         — manual deep check only

A full DB scan is never run per-second; periodic tier honors a minimum
interval. Deterministic maintenance only — AI explains results but may
not execute maintenance itself (§23).
"""
from __future__ import annotations

import time
from typing import Any, Callable

TIERS = ("incremental", "periodic", "event", "full")


class InvestmentMaintenanceService:
    def __init__(self, *, periodic_interval_s: float = 300.0) -> None:
        self._checks: dict[str, dict[str, Callable[[], dict[str, Any]]]] = {
            t: {} for t in TIERS}
        self._interval = float(periodic_interval_s)
        self._last_periodic = 0.0
        self._runs: list[dict[str, Any]] = []

    def register(self, tier: str, name: str,
                 fn: Callable[[], dict[str, Any]]) -> None:
        assert tier in TIERS
        self._checks[tier][name] = fn

    # --------------------------------------------------------------
    def run_tier(self, tier: str, *, force: bool = False) -> dict[str, Any]:
        if tier not in TIERS:
            return {"ok": False, "error_code": "TIER_UNKNOWN"}
        if tier == "periodic" and not force:
            if time.time() - self._last_periodic < self._interval:
                return {"ok": True, "skipped": "interval_not_due",
                        "next_due_in_s": round(
                            self._interval
                            - (time.time() - self._last_periodic), 1)}
        if tier == "periodic":
            self._last_periodic = time.time()
        results: dict[str, Any] = {}
        ok = True
        for name, fn in self._checks[tier].items():
            try:
                r = fn()
            except Exception as exc:
                r = {"ok": False, "error_code": type(exc).__name__}
            results[name] = r
            ok = ok and r.get("ok", True)
        run = {"ok": ok, "tier": tier, "at": time.time(),
               "checks": results}
        self._runs.append(run)
        self._runs = self._runs[-50:]
        return run

    def on_event(self, name: str) -> dict[str, Any]:
        fn = self._checks["event"].get(name)
        if not fn:
            return {"ok": False, "error_code": "EVENT_CHECK_UNKNOWN"}
        try:
            return {"ok": True, "check": name, "result": fn()}
        except Exception as exc:
            return {"ok": False, "check": name,
                    "error_code": type(exc).__name__}

    # --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {"ok": True,
                "tiers": {t: sorted(c) for t, c in self._checks.items()},
                "periodic_interval_s": self._interval,
                "recent_runs": list(self._runs[-5:]),
                "note": "確定性維護——AI 僅分析/說明，不執行維護"}
