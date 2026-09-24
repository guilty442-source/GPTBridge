"""InvestmentMonitoringScheduler — calendar/event/config driven slots.

Wraps the intelligence scheduler's due-slot logic (market calendar, not
wall-clock polling) and adds job-idempotency: the same slot for the same
data vintage runs at most once — ``mark_ran`` records the vintage.
No busy polling: callers ask ``due()`` and run what's returned.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class InvestmentMonitoringScheduler:
    def __init__(self, intel_scheduler: Any, state_dir: Path) -> None:
        self._sched = intel_scheduler
        self._path = state_dir / "monitoring" / "runs.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._runs = {k: float(v) for k, v in json.loads(
                self._path.read_text(encoding="utf-8")).items()}
        except Exception:
            self._runs = {}

    def _persist(self) -> None:
        self._path.write_text(json.dumps(self._runs), encoding="utf-8")

    # ------------------------------------------------------------------
    def due(self, market_date_fresh: dict[str, str] | None = None
            ) -> dict[str, Any]:
        """Slots due — skipped automatically when market data for that
        market hasn't refreshed since the last run."""
        slots = self._sched.due_slots(
            market_date_fresh=market_date_fresh)
        runnable = []
        for s in slots:
            key = f"{s['schedule_id']}|{s.get('market_date', '')}"
            if key in self._runs:
                continue                    # idempotent — ran already
            runnable.append({**s, "run_key": key})
        return {"ok": True, "due": runnable}

    def mark_ran(self, schedule_id: str, market_date: str,
                 run_key: str | None = None) -> dict[str, Any]:
        r = self._sched.mark_ran(schedule_id, market_date)
        if run_key:
            self._runs[run_key] = time.time()
            self._persist()
        return r

    def status(self) -> dict[str, Any]:
        return {"ok": True,
                "schedules": self._sched.list(),
                "completed_runs": len(self._runs)}
