"""TradingFailureController — degraded-mode policy for live trading.

Failures never silently unblock: each recorded fault kind latches a
degraded flag until an explicit ``clear`` (operator action) plus a
reconciliation pass. ``can_open_new`` is consulted before every new
submission — any hard fault refuses new risk while keeping
reconciliation, audit, and emergency controls fully operational.

Model failure degrades ADVISORY only — risk evaluation, order tracking,
reconciliation and emergency stop do not depend on the model.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

_HARD_FAULTS = frozenset({
    "broker_disconnect", "market_data_down", "postgres_failure",
    "channel_failure", "restart_unclean", "report_lost",
})
_ADVISORY_FAULTS = frozenset({"model_failure"})


class TradingFailureController:
    def __init__(self, state_dir: Path, journal) -> None:
        self._path = Path(state_dir) / "live_failures.json"
        self._journal = journal
        self._faults: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        import json
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._faults = data
        except Exception:
            self._faults = {}

    def _persist(self) -> None:
        import json
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._faults, indent=1,
                                  ensure_ascii=False), "utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    def report(self, kind: str, detail: str = "") -> dict[str, Any]:
        kind = str(kind)
        if kind not in _HARD_FAULTS and kind not in _ADVISORY_FAULTS:
            return {"ok": False, "error_code": "FAULT_KIND_UNKNOWN"}
        self._faults[kind] = {"kind": kind, "detail": str(detail),
                              "at": time.time()}
        self._persist()
        self._journal("live_audit", {
            "event_type": "failure.reported", "result": kind,
            "detail": {"detail": str(detail)}})
        return {"ok": True, "kind": kind,
                "severity": "hard" if kind in _HARD_FAULTS
                            else "advisory"}

    def clear(self, kind: str, reconciled: bool = False
              ) -> dict[str, Any]:
        """Clearing a hard fault requires a completed reconciliation."""
        kind = str(kind)
        if kind in _HARD_FAULTS and not reconciled:
            return {"ok": False,
                    "error_code": "RECONCILIATION_REQUIRED_FIRST"}
        if self._faults.pop(kind, None) is None:
            return {"ok": False, "error_code": "FAULT_NOT_ACTIVE"}
        self._persist()
        return {"ok": True, "cleared": kind}

    def can_open_new(self) -> bool:
        return not any(k in _HARD_FAULTS for k in self._faults)

    def status(self) -> dict[str, Any]:
        return {"faults": list(self._faults.values()),
                "can_open_new": self.can_open_new(),
                "advisory_degraded": "model_failure" in self._faults}
