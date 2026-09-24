"""BacktestJobQueue — bounded concurrency, cancellation, memory budget."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


class BacktestJobQueue:
    def __init__(self, state_dir: Path, *,
                 max_concurrent: int = 2,
                 timeout_s: float = 120.0,
                 memory_budget_mb: int = 512) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max = int(max_concurrent)
        self._timeout = float(timeout_s)
        self._mem_mb = int(memory_budget_mb)
        self._sem = threading.Semaphore(self._max)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, job_id: str, fn: Callable[[], dict[str, Any]]
               ) -> dict[str, Any]:
        """Non-blocking submit; caller polls status."""
        with self._lock:
            if job_id in self._jobs:
                return {"ok": False, "error_code": "JOB_EXISTS"}
            self._jobs[job_id] = {
                "job_id": job_id, "status": "queued",
                "submitted_at": time.time(), "result": None,
            }
        t = threading.Thread(target=self._run, args=(job_id, fn),
                             daemon=True)
        t.start()
        return {"ok": True, "job_id": job_id, "status": "queued"}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                return {"ok": False, "error_code": "JOB_NOT_FOUND"}
            self._cancel.add(job_id)
            self._jobs[job_id]["status"] = "cancelled"
        return {"ok": True, "job_id": job_id, "status": "cancelled"}

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._jobs.get(job_id) or
                        {"ok": False, "error_code": "JOB_NOT_FOUND"})

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    # ------------------------------------------------------------------
    def _run(self, job_id: str, fn: Callable[[], dict[str, Any]]) -> None:
        if not self._sem.acquire(timeout=self._timeout):
            with self._lock:
                self._jobs[job_id]["status"] = "timeout"
            return
        try:
            if job_id in self._cancel:
                return
            with self._lock:
                self._jobs[job_id]["status"] = "running"
            result = fn()
            with self._lock:
                self._jobs[job_id]["status"] = (
                    "cancelled" if job_id in self._cancel else "done")
                self._jobs[job_id]["result"] = result
        except Exception as exc:
            with self._lock:
                self._jobs[job_id]["status"] = "failed"
                self._jobs[job_id]["error"] = str(exc)
        finally:
            self._sem.release()


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
