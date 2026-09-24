"""BrokerOfflineGate — offline-phase safety boundary.

Hard policy for this phase:

- ``broker_network_enabled = false``
- ``live_trading_enabled = false``
- ``broker_authentication_enabled = false``

Defense in depth — NOT a single boolean:

1. The flags live in a governed config artifact
   (``runtime/state/broker-offline-gate.json``) written only through the
   governed configuration path; this service never exposes a setter to
   AI/front-end/strategy callers — any mutation attempt returns
   ``OFFLINE_GATE_LOCKED``.
2. Real adapters hold no transport implementation — there is no HTTP
   client/socket attribute on Cathay/Fubon adapters to begin with, so a
   stray ``place_order`` call can only reach a denial.
3. Every gateway dispatch additionally consults this gate, so even a
   hypothetical verified-adapter path stays closed while offline.

The gate is intentionally not disable-able through any command surface;
flipping it requires editing the governed artifact by an authorized
operator, which is an auditable out-of-band action.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


DEFAULTS = {
    "broker_network_enabled": False,
    "live_trading_enabled": False,
    "broker_authentication_enabled": False,
}


class BrokerOfflineGate:
    """Read-only view of the governed offline policy + deny helper."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "broker-offline-gate.json"
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(
                {**DEFAULTS, "set_by": "governed-default",
                 "at": time.time()}, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def _policy(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(DEFAULTS)
                merged.update({k: bool(v) for k, v in data.items()
                               if k in DEFAULTS})
                merged["set_by"] = data.get("set_by")
                return merged
        except Exception:
            pass
        return dict(DEFAULTS)

    def status(self) -> dict[str, Any]:
        p = self._policy()
        return {
            "ok": True,
            "broker_network_enabled": p["broker_network_enabled"],
            "live_trading_enabled": p["live_trading_enabled"],
            "broker_authentication_enabled":
                p["broker_authentication_enabled"],
            "set_by": p.get("set_by"),
            "mutable_by": "governed-config-only",
        }

    # ------------------------------------------------------------------
    def network_allowed(self) -> bool:
        return self._policy()["broker_network_enabled"]

    def live_allowed(self) -> bool:
        p = self._policy()
        return (p["live_trading_enabled"]
                and p["broker_network_enabled"]
                and p["broker_authentication_enabled"])

    def check_dispatch(self) -> dict[str, Any]:
        """Called before any adapter dispatch — deny when offline."""
        if not self.live_allowed():
            return {"ok": False,
                    "error_code": "BROKER_INTEGRATION_DISABLED",
                    "policy": self.status()}
        return {"ok": True}

    def attempt_mutation(self, actor: str, key: str,
                         value: Any) -> dict[str, Any]:
        """AI/front-end/strategy mutation attempts land here — denied."""
        return {
            "ok": False,
            "error_code": "OFFLINE_GATE_LOCKED",
            "actor": actor,
            "key": key,
            "note": "offline policy changes require the governed "
                    "configuration path, not a runtime command",
        }
