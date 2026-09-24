"""EmergencyTradingControl — halt switches decoupled from trade auth.

Actions are explicit and distinct:
- ``stop_new_orders`` blocks new submissions in a scope.
- ``cancel_open`` is a separate operator request — it only *requests*
  cancels through the gateway; it never implies liquidation.
- ``liquidate`` is a separate, heavier authorized operation — never
  equated with emergency stop.

Engage may be done by operator or automation rule; RELEASE requires an
authorized actor (``governor``/``user``/``risk-officer``) — AI can never
release an emergency stop.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .contracts import EmergencyEvent, EmergencyScope

_RELEASE_ISSUERS = frozenset({"governor", "user", "risk-officer"})
_AI_ACTORS = frozenset({
    "ai", "xingcheng", "model", "llm", "assistant", "agent",
    "local-model", "星澄"})


class EmergencyTradingControl:
    def __init__(self, journal) -> None:
        self._journal = journal          # persistence.record
        self._halts: dict[str, EmergencyEvent] = {}
        self._load()

    def _load(self) -> None:
        for row in self._journal_read():
            ev = EmergencyEvent(**{
                k: v for k, v in row.items()
                if k in EmergencyEvent.__dataclass_fields__})
            key = f"{ev.scope}:{ev.scope_key}"
            if ev.action == "release":
                self._halts.pop(key, None)
            elif ev.action == "stop_new_orders":
                self._halts[key] = ev

    def _journal_read(self) -> list[dict[str, Any]]:
        return self._journal("live_emergency_events", None)

    # ------------------------------------------------------------------
    def engage(self, scope: str, scope_key: str = "", by: str = "",
               reason: str = "") -> dict[str, Any]:
        try:
            sc = EmergencyScope(str(scope).upper())
        except ValueError:
            return {"ok": False, "error_code": "SCOPE_UNKNOWN"}
        ev = EmergencyEvent(scope=sc.value, scope_key=str(scope_key),
                            action="stop_new_orders", by=str(by or
                            "operator"), reason=str(reason))
        self._halts[f"{ev.scope}:{ev.scope_key}"] = ev
        self._journal("live_emergency_events", ev.to_dict())
        return {"ok": True, "event": ev.to_dict()}

    def release(self, scope: str, scope_key: str = "", by: str = "",
                reason: str = "") -> dict[str, Any]:
        if str(by).lower() in _AI_ACTORS:
            return {"ok": False, "error_code": "AI_CANNOT_RELEASE"}
        if str(by) not in _RELEASE_ISSUERS:
            return {"ok": False, "error_code": "RELEASE_NOT_AUTHORIZED"}
        key = f"{str(scope).upper()}:{scope_key}"
        if key not in self._halts:
            return {"ok": False, "error_code": "NO_ACTIVE_HALT"}
        ev = EmergencyEvent(scope=str(scope).upper(),
                            scope_key=str(scope_key),
                            action="release", by=str(by),
                            reason=str(reason))
        del self._halts[key]
        self._journal("live_emergency_events", ev.to_dict())
        return {"ok": True, "event": ev.to_dict()}

    # ------------------------------------------------------------------
    def is_halted(self, *, market: str = "", account_id: str = "",
                  strategy_id: str = "") -> bool:
        if f"{EmergencyScope.ALL.value}:" in self._halts:
            return True
        for scope, key in ((EmergencyScope.MARKET.value, market),
                           (EmergencyScope.ACCOUNT.value, account_id),
                           (EmergencyScope.STRATEGY.value, strategy_id)):
            if key and f"{scope}:{key}" in self._halts:
                return True
        return False

    def status(self) -> dict[str, Any]:
        return {"halts": [e.to_dict() for e in self._halts.values()],
                "count": len(self._halts)}
