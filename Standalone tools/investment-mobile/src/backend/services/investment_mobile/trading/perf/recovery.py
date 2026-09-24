"""InvestmentRecoveryCoordinator — governed restart sequence (§16).

    persist state → last event → pending work → open sim orders →
    sim cash → sim positions → risk state → strategy versions → policy

Never blindly replays signals after restart — deduped events/orders are
no-ops, running strategies land in RECOVERING→PAUSED (delegated to
AutoTradingMaintenanceService), and PAPER resumes only after checks.
"""
from __future__ import annotations

import time
from typing import Any, Callable


class InvestmentRecoveryCoordinator:
    def __init__(self, *, checks: dict[str, Callable[[], Any]] | None = None
                 ) -> None:
        # name -> callable returning {"ok": bool, ...details}
        self._checks = checks or {}
        self._runs: list[dict[str, Any]] = []

    def run(self, *, trigger: str = "startup") -> dict[str, Any]:
        order = ("persisted_state", "last_event", "pending_work",
                 "open_sim_orders", "sim_cash", "sim_positions",
                 "risk_state", "strategy_versions")
        results: dict[str, Any] = {}
        started = time.time()
        for name in order:
            fn = self._checks.get(name)
            if fn is None:
                results[name] = {"ok": True, "skipped": True}
                continue
            try:
                r = fn()
                results[name] = r if isinstance(r, dict) else {"ok": bool(r)}
            except Exception as exc:
                results[name] = {"ok": False,
                                 "error_code": type(exc).__name__}
            if not results[name].get("ok", True):
                run = {"trigger": trigger, "at": started,
                       "ok": False, "failed_check": name,
                       "checks": results,
                       "elapsed_ms": round((time.time() - started) * 1000, 1)}
                self._runs.append(run)
                return {**run, "policy": "fail-closed — hold strategies "
                        "PAUSED until resolved",
                        "note": "禁止重啟後盲目重放全部交易訊號"}
        run = {"trigger": trigger, "at": started, "ok": True,
               "checks": results,
               "elapsed_ms": round((time.time() - started) * 1000, 1)}
        self._runs.append(run)
        return run

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._runs[-limit:])
