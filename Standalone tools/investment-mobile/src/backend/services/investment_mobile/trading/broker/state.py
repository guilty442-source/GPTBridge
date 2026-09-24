"""BrokerConnectionState — per-broker connection lifecycle.

Phase rule: CONNECTED / DEGRADED / DISCONNECTED are unreachable — the
integration contracts exist but no transport is wired and the offline
gate refuses any transition into them. READY_FOR_INTEGRATION means only
that the local contract surface is complete; it does NOT imply broker
authorization was ever obtained.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import Any


class BrokerConnectionState(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    OFFLINE = "OFFLINE"
    MOCK = "MOCK"
    READY_FOR_INTEGRATION = "READY_FOR_INTEGRATION"
    CONNECTED = "CONNECTED"          # unreachable this phase
    DEGRADED = "DEGRADED"            # unreachable this phase
    DISCONNECTED = "DISCONNECTED"    # unreachable this phase


# States reachable while broker_network_enabled=false. Anything else is
# denied regardless of caller.
REACHABLE_OFFLINE: frozenset[BrokerConnectionState] = frozenset({
    BrokerConnectionState.NOT_CONFIGURED,
    BrokerConnectionState.OFFLINE,
    BrokerConnectionState.MOCK,
    BrokerConnectionState.READY_FOR_INTEGRATION,
})


class BrokerConnectionTracker:
    """Persisted per-broker connection state — fail-closed transitions."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "broker-connections.json"
        self._states: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._states = {str(k): v for k, v in data.items()
                                if isinstance(v, dict)}
        except Exception:
            self._states = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._states, indent=2, ensure_ascii=False),
            encoding="utf-8")

    # ------------------------------------------------------------------
    def get(self, broker_id: str) -> dict[str, Any]:
        row = self._states.get(str(broker_id))
        if row is None:
            return {"broker_id": str(broker_id),
                    "state": BrokerConnectionState.NOT_CONFIGURED.value,
                    "at": 0.0}
        return dict(row)

    def set(self, broker_id: str, state: BrokerConnectionState | str,
            *, actor: str = "system", reason: str = "") -> dict[str, Any]:
        target = BrokerConnectionState(str(state))
        if target not in REACHABLE_OFFLINE:
            return {
                "ok": False,
                "error_code": "CONNECTION_STATE_UNREACHABLE",
                "broker_id": str(broker_id),
                "requested": target.value,
                "state": self.get(broker_id)["state"],
            }
        self._states[str(broker_id)] = {
            "broker_id": str(broker_id),
            "state": target.value,
            "actor": actor,
            "reason": reason,
            "at": time.time(),
        }
        self._persist()
        return {"ok": True, "broker_id": str(broker_id),
                "state": target.value}
