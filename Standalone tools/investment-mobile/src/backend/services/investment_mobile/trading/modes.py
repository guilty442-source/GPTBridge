"""Trading mode gate — ANALYSIS/SHADOW/PAPER/LIVE.

LIVE startup preconditions (ALL required, fail-closed):
- explicit human authorization artifact (non-expired, strategy-scoped)
- target broker adapter ``api_verified`` (checked at dispatch time)
- risk limits configured (``risk-limits.json`` loadable)
- audit journal writable

Even if the user requests LIVE, missing preconditions refuse the switch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import TradingMode


class ModeGate:
    """Fail-closed trading-mode state machine."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._mode_path = state_dir / "trading-mode.json"
        self._auth_path = state_dir / "live-authorization.json"
        self._risk_limits_path = state_dir / "risk-limits.json"
        self._audit_path = state_dir / "trading-audit.jsonl"
        self._mode = TradingMode.ANALYSIS
        self._load()

    # ------------------------------------------------------------------
    @property
    def mode(self) -> TradingMode:
        return self._mode

    def _load(self) -> None:
        try:
            data = json.loads(self._mode_path.read_text(encoding="utf-8"))
            self._mode = TradingMode(str(data.get("mode", "ANALYSIS")))
        except Exception:
            self._mode = TradingMode.ANALYSIS

    def _persist(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._mode_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"mode": self._mode.value}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._mode_path)

    # ------------------------------------------------------------------
    def live_authorization(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._auth_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        expires = float(data.get("expires_at") or 0)
        if expires <= time.time():
            return None
        if not data.get("granted_by") or not data.get("strategy_ids"):
            return None
        return data

    def live_readiness(self) -> dict[str, Any]:
        """Enumerate LIVE preconditions — all must hold."""
        checks: dict[str, bool] = {}
        checks["human_authorization"] = self.live_authorization() is not None
        try:
            limits = json.loads(self._risk_limits_path.read_text(encoding="utf-8"))
            checks["risk_limits_configured"] = isinstance(limits, dict) and bool(
                limits.get("max_order_notional")
            )
        except Exception:
            checks["risk_limits_configured"] = False
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8"):
                pass
            checks["audit_ready"] = True
        except OSError:
            checks["audit_ready"] = False
        return {
            "ready": all(checks.values()),
            "checks": checks,
            # Broker API verification is per-adapter and enforced at
            # dispatch; listed here for the operator surface.
            "broker_api_verification": "per-adapter-at-dispatch",
        }

    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> dict[str, Any]:
        try:
            target = TradingMode(str(mode).upper())
        except ValueError:
            return {"ok": False, "error_code": "MODE_UNKNOWN", "mode": self._mode.value}
        if target is TradingMode.LIVE:
            readiness = self.live_readiness()
            if not readiness["ready"]:
                return {
                    "ok": False,
                    "error_code": "LIVE_PRECONDITIONS_UNMET",
                    "mode": self._mode.value,
                    "readiness": readiness,
                }
        self._mode = target
        self._persist()
        return {"ok": True, "mode": self._mode.value}

    # ------------------------------------------------------------------
    def allows(self, stage: str) -> bool:
        """Stage gates:

        - analysis/intents: always (signals, proposals, risk evaluation)
        - decisions: SHADOW and above — a formal risk-decided order
          record is produced
        - orders: PAPER and LIVE — SHADOW stops before order submission
        - fills: PAPER (simulated account) only
        - execution: broker dispatch — LIVE only
        """
        if stage in ("analysis", "intents"):
            return True
        if stage == "decisions":
            return self._mode in (
                TradingMode.SHADOW,
                TradingMode.PAPER,
                TradingMode.LIVE,
            )
        if stage == "orders":
            return self._mode in (TradingMode.PAPER, TradingMode.LIVE)
        if stage == "fills":
            return self._mode is TradingMode.PAPER
        if stage == "execution":
            return self._mode is TradingMode.LIVE
        return False
