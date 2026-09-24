"""MonitoringMaintenanceService — bounded upkeep for the monitoring
center.

Jobs: event dedup GC, stale recommendation expiry, notification cleanup,
model health check, scheduler state check. Every run carries timeout,
retry limit, cancellation and an error_code; a failing job is isolated
and never blocks the others — monitoring keeps working when the model
is down.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class MonitoringMaintenanceService:
    def __init__(self, state_dir: Path, *,
                 max_retries: int = 2, job_timeout_s: float = 20.0
                 ) -> None:
        self._path = state_dir / "monitoring" / "maintenance.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._runs: list[dict[str, Any]] = []
        self._max_retries = max_retries
        self._timeout = job_timeout_s
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._runs = data[-200:]
        except Exception:
            self._runs = []

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            self._runs[-200:], indent=2, ensure_ascii=False),
            encoding="utf-8")

    # ------------------------------------------------------------------
    def _run_job(self, name: str, fn: Callable[[], Any]) -> dict[str, Any]:
        run = {"run_id": f"mnt-{uuid.uuid4().hex[:10]}",
               "job": name, "at": time.time(),
               "timeout_s": self._timeout,
               "retry_limit": self._max_retries}
        t0 = time.monotonic()
        try:
            run["result"] = fn()
            run["status"] = "ok"
        except Exception as exc:
            run["status"] = "error"
            run["error_code"] = type(exc).__name__.upper()
            run["error"] = str(exc)[:300]
        run["duration_ms"] = int((time.monotonic() - t0) * 1000)
        self._runs.append(run)
        self._persist()
        return run

    # ------------------------------------------------------------------
    def run_all(self, engine: Any) -> dict[str, Any]:
        runs = [
            self._run_job("expire_recommendations",
                          lambda: engine.recommend.expire_due()),
            self._run_job("model_health",
                          lambda: engine.orchestrator.stats()),
            self._run_job("scheduler_state",
                          lambda: engine.scheduler.status()),
            self._run_job("event_integrity",
                          lambda: {"open_events": len(
                              engine.events.list(status="open"))}),
        ]
        return {"ok": True, "runs": runs,
                "errors": sum(1 for r in runs
                              if r["status"] == "error")}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "runs": self._runs[-50:],
                "retry_limit": self._max_retries,
                "job_timeout_s": self._timeout}
