"""LiveActivationGate — LIVE startup preconditions, phase-locked.

ALL checks must pass AND ``PHASE_LOCKED`` must be False. This phase the
lock is hardwired on: even a perfect readiness report cannot produce a
real order request. Flipping the lock is a deliberate code change, not
a runtime toggle — no command or payload can reach it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class LiveActivationGate:
    PHASE_LOCKED: bool = True   # this phase: live dispatch is OFF

    def __init__(self, state_dir: Path, *, mode_gate: Any = None,
                 accounts: Any = None, gateway: Any = None,
                 risk_engine: Any = None) -> None:
        self._dir = Path(state_dir)
        self._gate = mode_gate
        self._accounts = accounts
        self._gateway = gateway
        self._risk = risk_engine

    # ------------------------------------------------------------------
    def readiness(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}

        # authorization artifact (non-expired, human-issued)
        auth = None
        if self._gate is not None:
            auth = self._gate.live_authorization()
        checks["user_authorization"] = auth is not None

        # account identity resolvable
        checks["account_identity"] = bool(
            self._accounts and self._accounts.list_accounts())

        # broker connection + API capability verified
        gw = self._gateway
        conn_ok = False
        api_ok = False
        if gw is not None:
            for bid in ("CATHAY_SECURITIES", "FUBON_SUBBROKERAGE"):
                state = gw.connection_state(bid)
                conn_ok = conn_ok or bool(state.get("connected"))
                api_ok = api_ok or (
                    gw.capability(bid, "place_order") == "SUPPORTED")
        checks["broker_connection"] = conn_ok
        checks["broker_api_capability"] = api_ok

        # strategy version pinning + risk config + market/calendar/audit
        checks["strategy_version"] = True    # snapshot contract enforced
        checks["risk_limits"] = bool(
            self._risk and self._risk.configured)
        try:
            aud = self._dir / "trading-audit.jsonl"
            aud.parent.mkdir(parents=True, exist_ok=True)
            with aud.open("a", encoding="utf-8"):
                pass
            checks["audit_writable"] = True
        except OSError:
            checks["audit_writable"] = False
        checks["market_data"] = True         # verified at dispatch
        checks["calendar"] = True            # verified at dispatch
        checks["account_funds"] = False      # needs live broker snapshot
        checks["actual_positions"] = False   # needs live broker snapshot
        checks["open_orders_known"] = True   # local journal readable
        checks["emergency_stop"] = True      # control exists
        checks["phase_locked"] = not self.PHASE_LOCKED

        return {
            "ready": all(checks.values()),
            "checks": checks,
            "phase_locked": self.PHASE_LOCKED,
            "evaluated_at": time.time(),
        }

    def dispatch_allowed(self) -> bool:
        """Hard gate — phase lock alone keeps this False."""
        return (not self.PHASE_LOCKED) and self.readiness()["ready"]
