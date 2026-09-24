"""WindowsPowerStateHandler — sleep/wake/session/network handling (§17).

The tool never modifies Windows power policy. Events are fed in by the
host shell (or tests); on wake the handler runs the mandated check
sequence and only then reports whether PAPER autotrading may resume.
During suspend the tool reports SUSPENDED — it never pretends
strategies kept running.
"""
from __future__ import annotations

import time
from typing import Any, Callable

EVENTS = ("suspend", "resume", "logoff", "shutdown", "restart",
          "network_lost", "network_restored")


class WindowsPowerStateHandler:
    def __init__(self, *, checks: dict[str, Callable[[], bool]] | None = None
                 ) -> None:
        self._checks = checks or {}
        self._last_suspend: float | None = None
        self._events: list[dict[str, Any]] = []
        self.state = "ACTIVE"

    # --------------------------------------------------------------
    def handle(self, event: str) -> dict[str, Any]:
        ev = str(event).lower()
        if ev not in EVENTS:
            return {"ok": False, "error_code": "POWER_EVENT_UNKNOWN"}
        self._events.append({"event": ev, "at": time.time()})
        self._events = self._events[-100:]
        if ev == "suspend":
            self._last_suspend = time.time()
            self.state = "SUSPENDED"
            return {"ok": True, "state": self.state,
                    "note": "休眠期間策略視為暫停——不假裝持續運行"}
        if ev in ("logoff", "shutdown", "restart"):
            self.state = "STOPPING"
            return {"ok": True, "state": self.state,
                    "note": "工具應完成 checkpoint 並安全停止"}
        if ev == "network_lost":
            self.state = "NETWORK_DOWN"
            return {"ok": True, "state": self.state}
        return {"ok": True, "state": self.state}

    # --------------------------------------------------------------
    def resume_checks(self) -> dict[str, Any]:
        """Post-wake sequence: time sync → market calendar → market-data
        validity → pending events → sim account. Any failure keeps
        PAPER autotrading blocked (fail-closed)."""
        order = ("time_sync", "market_calendar", "market_data_valid",
                 "pending_events", "sim_account")
        results: dict[str, bool] = {}
        for name in order:
            fn = self._checks.get(name)
            try:
                results[name] = bool(fn()) if fn else True
            except Exception:
                results[name] = False
            if not results[name]:
                self.state = "RECOVERING"
                return {"ok": False, "error_code": "RESUME_CHECK_FAILED",
                        "failed_check": name, "checks": results,
                        "paper_allowed": False,
                        "note": "資料未恢復有效——不得恢復 PAPER 自動交易"}
        gap = (time.time() - self._last_suspend
               if self._last_suspend else 0.0)
        self.state = "ACTIVE"
        return {"ok": True, "checks": results,
                "suspend_gap_s": round(gap, 1), "paper_allowed": True}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "state": self.state,
                "last_suspend": self._last_suspend,
                "events": list(self._events[-10:]),
                "note": "不修改 Windows 全域電源政策"}
