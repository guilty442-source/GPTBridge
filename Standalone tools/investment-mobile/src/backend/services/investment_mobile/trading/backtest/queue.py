"""BacktestJobQueue — bounded queue + governor-sized worker pool.

bounded-concurrency/v1: ``submit`` admits into the shared
:class:`BoundedExecutor` primitive (capacity + priority + deadline +
backpressure + cancellation + drop/reject) — never a thread per job.
The effective worker count is the governor ``batch`` class quota
clamped into the declared envelope; missing/stale governor state falls
back to the static maximum (fail-open, A116).  ``_jobs`` keeps only the
newest records — terminal entries are evicted first (retention bound).
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import CancelledError, Future
from pathlib import Path
from typing import Any, Callable

from shared_layer.adaptive.bounded_executor import (
    AdmissionRejected,
    BoundedExecutor,
    OverflowPolicy,
    PoolPaused,
    PoolPolicy,
    WorkExpired,
)
from shared_layer.adaptive.types import PriorityClass


class BacktestJobQueue:
    _MAX_QUEUED = 64        # queue capacity (declared envelope)
    _MAX_JOB_RECORDS = 256  # retention bound for status records
    _TERMINAL = {"done", "failed", "timeout", "cancelled", "rejected"}

    def __init__(self, state_dir: Path, *,
                 max_concurrent: int = 2,
                 timeout_s: float = 120.0,
                 memory_budget_mb: int = 512) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._mem_mb = int(memory_budget_mb)
        # Envelope is module/caller-declared; the governor "batch" quota
        # decides the effective worker count inside it.
        self._executor = BoundedExecutor(PoolPolicy(
            pool="investment.backtest",
            work_class="batch",
            min_workers=1,
            max_workers=max(1, int(max_concurrent)),
            queue_capacity=self._MAX_QUEUED,
            deadline_ms=int(float(timeout_s) * 1000),
            overflow=OverflowPolicy.REJECT,
            backpressure_wait_ms=0,
        ))
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, job_id: str, fn: Callable[[], dict[str, Any]], *,
               priority: PriorityClass = PriorityClass.BACKGROUND,
               wait_ms: int = 0) -> dict[str, Any]:
        """Non-blocking submit (optional backpressure wait); caller polls."""
        with self._lock:
            if job_id in self._jobs:
                return {"ok": False, "error_code": "JOB_EXISTS"}
            self._jobs[job_id] = {
                "job_id": job_id, "status": "queued",
                "submitted_at": time.time(), "result": None,
            }
            self._evict_terminal_jobs()
            cancel = threading.Event()
            self._cancel[job_id] = cancel
        future = self._executor.submit(
            self._execute, job_id, fn,
            priority=priority, cancel=cancel, wait_ms=wait_ms,
        )
        with self._lock:
            self._futures[job_id] = future
        future.add_done_callback(lambda f: self._settle(job_id, f))
        exc = future.exception() if future.done() else None
        if isinstance(exc, PoolPaused):
            return {"ok": False, "error_code": "JOB_PAUSED",
                    "job_id": job_id}
        if isinstance(exc, (AdmissionRejected, WorkExpired, CancelledError)):
            return {"ok": False, "error_code": "JOB_QUEUE_FULL",
                    "job_id": job_id}
        return {"ok": True, "job_id": job_id, "status": "queued"}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                return {"ok": False, "error_code": "JOB_NOT_FOUND"}
            event = self._cancel.get(job_id)
            future = self._futures.get(job_id)
        if event is not None:
            event.set()
        if future is not None:
            future.cancel()  # retires queued work before dispatch
        with self._lock:
            self._jobs[job_id]["status"] = "cancelled"
        return {"ok": True, "job_id": job_id, "status": "cancelled"}

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._jobs.get(job_id) or
                        {"ok": False, "error_code": "JOB_NOT_FOUND"})

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    def metrics(self) -> dict[str, Any]:
        return self._executor.metrics()

    def shutdown(self, *, drain: bool = True) -> None:
        """Cancellation-aware drain: workers finish in-flight, then exit."""
        self._executor.shutdown(drain=drain)

    # ------------------------------------------------------------------
    def _execute(self, job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "queued":
                return
            job["status"] = "running"
        try:
            result = fn()
        except Exception as exc:
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)
            return
        with self._lock:
            cancelled = self._cancel[job_id].is_set()
            job["status"] = "cancelled" if cancelled else "done"
            job["result"] = None if cancelled else result

    def _settle(self, job_id: str, future: Future) -> None:
        """Map executor terminal states for work that never dispatched."""
        status = None
        if future.cancelled():
            status = "cancelled"
        else:
            exc = future.exception()
            if isinstance(exc, WorkExpired):
                status = "timeout"
            elif isinstance(exc, (AdmissionRejected, PoolPaused)):
                status = "rejected"
            elif exc is not None:
                status = "failed"
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and status is not None and (
                    job["status"] == "queued"):
                job["status"] = status
                if status == "failed":
                    job["error"] = str(future.exception())
            self._futures.pop(job_id, None)
            if job is not None and job["status"] in self._TERMINAL:
                self._cancel.pop(job_id, None)

    def _evict_terminal_jobs(self) -> None:
        """Retention bound: drop-oldest terminal records past the cap."""
        if len(self._jobs) < self._MAX_JOB_RECORDS:
            return
        stale = sorted(
            (j for j in self._jobs.values()
             if j["status"] in self._TERMINAL),
            key=lambda j: j["submitted_at"],
        )
        for job in stale[: len(self._jobs) - self._MAX_JOB_RECORDS + 1]:
            del self._jobs[job["job_id"]]
            self._cancel.pop(job["job_id"], None)
            self._futures.pop(job["job_id"], None)


class BacktestMaintenance:
    """Marks results stale when source data is revised."""

    def __init__(self, state_dir: Path,
                 revision_feed: Callable[[], list[str]]) -> None:
        self._path = Path(state_dir) / "backtest_results.jsonl"
        self._revisions = revision_feed

    def run_once(self) -> dict[str, Any]:
        """Any revision newer than a result's data_revision → stale."""
        revised_ids = set(self._revisions())
        marked = []
        if not self._path.exists():
            return {"ok": True, "stale_marked": marked}
        lines = self._path.read_text("utf-8").splitlines()
        rows = [json.loads(l) for l in lines if l.strip()]
        for row in rows:
            scope = (row.get("config") or {}).get("instrument_scope") or []
            if any(i in revised_ids for i in scope):
                if not row.get("stale"):
                    row["stale"] = True
                    marked.append(row.get("run_id"))
        self._path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
            + "\n", "utf-8")
        return {"ok": True, "stale_marked": marked,
                "note": "資料修正後舊回測標記為 stale，不得再視為最新資料結果"}
