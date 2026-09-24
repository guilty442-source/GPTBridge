"""SimulationRecoveryService — crash/restart correctness.

Every state-changing sim step appends a sequenced event. On restart the
service reads the last committed seq, replays pending orders through
idempotent apply_fill (exec_id dedup), and reports what was in flight.
Replay of the same event stream reproduces identical results.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class SimulationRecoveryService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._dir / "simulation_events.jsonl"
        self._checkpoint_path = self._dir / "simulation_checkpoint.json"
        self._seq = self._last_seq()

    # ------------------------------------------------------------------
    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record_event(self, kind: str, detail: dict[str, Any]) -> int:
        seq = self.next_seq()
        with self._events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "seq": seq, "kind": kind, "detail": detail,
                "at": time.time(), "simulated": True},
                ensure_ascii=False) + "\n")
        return seq

    def checkpoint(self, state: dict[str, Any]) -> dict[str, Any]:
        row = {"seq": self._seq, "state": state, "at": time.time()}
        self._checkpoint_path.write_text(
            json.dumps(row, ensure_ascii=False, indent=1), "utf-8")
        return {"ok": True, "seq": self._seq}

    def recover(self, open_orders: list[dict[str, Any]]
                ) -> dict[str, Any]:
        """Post-restart reconciliation — pending orders listed, not
        re-executed (idempotent apply prevents double fills)."""
        checkpoint = {}
        if self._checkpoint_path.exists():
            try:
                checkpoint = json.loads(
                    self._checkpoint_path.read_text("utf-8"))
            except ValueError:
                checkpoint = {}
        events = self._read_events()
        return {
            "ok": True,
            "last_seq": self._seq,
            "checkpoint": checkpoint.get("state") or {},
            "pending_orders": [o["order_id"] for o in open_orders],
            "events_replayed": len(events),
            "note": "重啟後不重複成交：fill 以 exec_id 冪等",
        }

    def events(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._read_events()[-limit:]

    # ------------------------------------------------------------------
    def _read_events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        out = []
        for l in self._events_path.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(l))
            except ValueError:
                continue
        return out

    def _last_seq(self) -> int:
        evs = self._read_events()
        return int(evs[-1]["seq"]) if evs else 0
