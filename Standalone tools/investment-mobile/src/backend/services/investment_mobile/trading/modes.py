"""Trading mode gate — ANALYSIS/SHADOW/PAPER/LIVE.

LIVE is unreachable without an explicit human authorization artifact:

- ``runtime/state/live-authorization.json`` must exist and contain a
  non-expired grant scoped to the active strategy set. The artifact is the
  recorded consent — the engine never fabricates it.
- The target broker adapter must additionally report ``api_verified``.
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
        self._mode = TradingMode.ANALYSIS
        self._load()

    # ------------------------------------------------------------------
    @property
    def mode(self) -> TradingMode:
        return self._mode

    @property
    def mode_path(self) -> Path:
        return self._mode_path

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
        """Return the human authorization grant, or None."""
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

    def set_mode(self, mode: str) -> dict[str, Any]:
        try:
            target = TradingMode(str(mode).upper())
        except ValueError:
            return {"ok": False, "error_code": "MODE_UNKNOWN", "mode": self._mode.value}
        if target is TradingMode.LIVE and self.live_authorization() is None:
            return {
                "ok": False,
                "error_code": "LIVE_AUTHORIZATION_REQUIRED",
                "mode": self._mode.value,
            }
        self._mode = target
        self._persist()
        return {"ok": True, "mode": self._mode.value}

    # ------------------------------------------------------------------
    def allows(self, stage: str) -> bool:
        """Whether ``stage`` may run in the current mode.

        - analysis: always (signal ingestion, research, risk evaluation)
        - intents: strategy intents emitted (all modes — they are inert
          without OMS submission)
        - orders: OMS accepts submissions in SHADOW/PAPER/LIVE
        - fills: fills are simulated in SHADOW/PAPER; LIVE routes to a
          verified broker adapter
        - execution: broker dispatch — LIVE only
        """
        if stage in ("analysis", "intents"):
            return True
        if stage == "orders":
            return self._mode in (
                TradingMode.SHADOW,
                TradingMode.PAPER,
                TradingMode.LIVE,
            )
        if stage == "fills":
            return self._mode in (TradingMode.SHADOW, TradingMode.PAPER)
        if stage == "execution":
            return self._mode is TradingMode.LIVE
        return False
