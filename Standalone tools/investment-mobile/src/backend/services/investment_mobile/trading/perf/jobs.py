"""InvestmentJobManager — one bounded registry for all background work.

Every job carries job_id/job_type/priority/status/timestamps/
retry_count/error_code plus timeout and cancellation (§14). No
unbounded background work: the queue cap comes from
InvestmentResourceBudget; full queue rejects with JOB_QUEUE_FULL.
"""
from __future__ import annotations

import itertools
import time
from typing import Any, Callable

STATUS = ("QUEUED", "RUNNING", "COMPLETED", "FAILED",
          "CANCELLED", "TIMED_OUT")


class InvestmentJobManager:
    def __init__(self, *, max_jobs: int = 32, max_retries: int = 2,
                 default_timeout_s: float = 60.0) -> None:
        self._max = max(1, int(max_jobs))
        self._max_retries = max_retries
        self._timeout = float(default_timeout_s)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._cancel: set[str] = set()

    # --------------------------------------------------------------
    def submit(self, job_type: str, *, priority: int = 5,
               payload: dict[str, Any] | None = None,
               timeout_s: float | None = None,
               idem_key: str = "") -> dict[str, Any]:
        active = [j for j in self._jobs.values()
                  if j["status"] in ("QUEUED", "RUNNING")]
        if idem_key:
            for j in active:
                if j["idem_key"] == idem_key:
                    return {"ok": True, "deduplicated": True,
                            "job_id": j["job_id"]}
        if len(active) >= self._max:
            return {"ok": False, "error_code": "JOB_QUEUE_FULL",
                    "in_flight": len(active)}
        jid = f"job-{next(self._ids)}"
        self._jobs[jid] = {
            "job_id": jid, "job_type": str(job_type),
            "priority": int(priority), "status": "QUEUED",
            "created_at": time.time(), "started_at": None,
            "completed_at": None, "retry_count": 0,
            "error_code": "", "idem_key": idem_key,
            "timeout_s": float(timeout_s or self._timeout),
            "payload": dict(payload or {}),
        }
        return {"ok": True, "job_id": jid}

    # --------------------------------------------------------------
    def run_next(self, fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Run the highest-priority queued job synchronously under its
        timeout. Cooperative cancellation via is_cancelled()."""
        queued = [j for j in self._jobs.values() if j["status"] == "QUEUED"]
        if not queued:
            return {"ok": False, "error_code": "NO_QUEUED_JOB"}
        job = min(queued, key=lambda j: (j["priority"], j["created_at"]))
        job["status"] = "RUNNING"
        job["started_at"] = time.time()
        try:
            result = fn(job)
            if job["job_id"] in self._cancel:
                job["status"] = "CANCELLED"
                job["error_code"] = "CANCELLED"
            elif (time.time() - job["started_at"]) > job["timeout_s"]:
                job["status"] = "TIMED_OUT"
                job["error_code"] = "TIMEOUT"
            else:
                job["status"] = "COMPLETED"
                job["result"] = result
        except Exception as exc:
            job["error_code"] = type(exc).__name__
            if job["retry_count"] < self._max_retries and \
                    job["job_id"] not in self._cancel:
                job["retry_count"] += 1
                job["status"] = "QUEUED"
            else:
                job["status"] = "FAILED"
        finally:
            if job["status"] not in ("QUEUED", "RUNNING"):
                job["completed_at"] = time.time()
            self._cancel.discard(job["job_id"])
        return {"ok": True, "job_id": job["job_id"],
                "status": job["status"]}

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(str(job_id))
        if not job:
            return {"ok": False, "error_code": "JOB_UNKNOWN"}
        if job["status"] in ("COMPLETED", "FAILED", "TIMED_OUT"):
            return {"ok": False, "error_code": "JOB_FINISHED"}
        self._cancel.add(job["job_id"])
        if job["status"] == "QUEUED":
            job["status"] = "CANCELLED"
            job["completed_at"] = time.time()
        return {"ok": True, "job_id": job["job_id"], "status": job["status"]}

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancel

    def reap(self, *, older_than_s: float = 3600.0) -> int:
        """Drop finished jobs older than the bound — registry stays
        bounded over long runs."""
        cutoff = time.time() - older_than_s
        done = [jid for jid, j in self._jobs.items()
                if j["status"] not in ("QUEUED", "RUNNING")
                and (j["completed_at"] or 0) < cutoff]
        for jid in done:
            del self._jobs[jid]
        return len(done)

    # --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        by = {s: 0 for s in STATUS}
        for j in self._jobs.values():
            by[j["status"]] = by.get(j["status"], 0) + 1
        return {"ok": True, "jobs": by, "capacity": self._max,
                "total": len(self._jobs)}
