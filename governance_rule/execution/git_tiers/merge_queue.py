"""Durable, fair merge queue (task §20/§21, A375 MERGE-QUEUE).

Each entry binds ``worker_id``, ``source_branch`` and an *immutable*
``source_commit`` SHA plus the ``base_main_commit`` observed at enqueue —
the coordinator merges the approved SHA, never a moving branch.  When a
branch HEAD moves after enqueue, the entry is stale: it must be cancelled
and re-enqueued with the new SHA; a stale approval is never reused.

Ordering: higher ``priority`` first, then ``enqueue_sequence`` (FIFO within
a priority).  Workers cannot self-elevate: ``priority`` above NORMAL is
accepted only with an explicit ``escalated_by`` governed actor, recorded in
the entry and the audit ledger.

Retry policy: one failed attempt marks the entry ``blocked`` with
``blocked_reason`` — the next sync cycle must not silently retry it.  A
``conflicted`` entry waits for a human or a fresh enqueue.

States: pending | running | merged | conflicted | blocked | failed |
cancelled.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import audit_log
from .audit_chain import chained_audit_log
from .branch_policy import policy_digest
from .git_repository import GitRepository
from .process_lock import ProcessFileLock, LockBusyError

PRIORITY_NORMAL: int = 0
PRIORITY_URGENT: int = 10
QUEUE_STATUSES = frozenset(
    {"pending", "running", "merged", "conflicted", "blocked", "failed", "cancelled"}
)
TERMINAL_STATUSES = frozenset({"merged", "failed", "cancelled"})
ESCALATION_ACTORS = frozenset(
    {
        "human-governor",
        "governance/automation-supervisor",
        "governance/git-coordinator",
        "governance/workspace-sync",
        "governance/decision-layer",
    }
)


def _queue_dir(root: str | Path) -> Path:
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    directory = common.resolve() / "gptbridge-automation" / "merge-queue"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _now() -> float:
    return time.time()


class MergeQueue:
    """File-backed merge queue scoped to one repository."""

    QUEUE_FILE = "queue.json"

    def __init__(self, root: str | Path) -> None:
        self._dir = _queue_dir(root)
        self._file = self._dir / self.QUEUE_FILE
        self._lock_path = self._dir / "merge-queue.lock"

    # -- persistence ----------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("next_sequence", 1)
        payload.setdefault("entries", [])
        return payload

    def _store(self, payload: dict[str, Any]) -> None:
        tmp = self._file.with_name(
            self._file.name + f".{os.getpid()}.tmp"
        )
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._file)

    @staticmethod
    def _entry(items: list[dict[str, Any]], queue_id: str) -> dict[str, Any] | None:
        for item in items:
            if item.get("queue_id") == queue_id:
                return item
        return None

    # -- enqueue --------------------------------------------------------

    def enqueue(
        self,
        worker_id: str,
        branch: str,
        source_commit: str,
        *,
        base_main_commit: str = "",
        task_id: str = "",
        priority: int = PRIORITY_NORMAL,
        escalated_by: str = "",
        not_before: float = 0.0,
    ) -> dict[str, Any]:
        """Bind one immutable source SHA to a queue slot.

        A priority above NORMAL requires ``escalated_by`` to be a governed
        actor; otherwise the entry is created at NORMAL (no self-elevation).
        """
        priority = int(priority)
        if priority > PRIORITY_NORMAL and escalated_by not in ESCALATION_ACTORS:
            priority = PRIORITY_NORMAL
        audit_id = uuid.uuid4().hex[:16]
        try:
            with ProcessFileLock(self._lock_path):
                payload = self._load()
                sequence = int(payload["next_sequence"])
                entry = {
                    "queue_id": f"mq-{sequence:06d}-{audit_id[:6]}",
                    "enqueue_sequence": sequence,
                    "worker_id": str(worker_id),
                    "task_id": str(task_id),
                    "source_branch": str(branch),
                    "source_commit": str(source_commit),
                    "base_main_commit": str(base_main_commit),
                    "enqueue_time": _now(),
                    "priority": priority,
                    "escalated_by": str(escalated_by) if priority > PRIORITY_NORMAL else "",
                    "attempt_count": 0,
                    "not_before": float(not_before or 0.0),
                    "blocked_reason": "",
                    "status": "pending",
                    "audit_id": audit_id,
                    "policy_hash": policy_digest(),
                    "updated_at": _now(),
                }
                payload["next_sequence"] = sequence + 1
                payload["entries"].append(entry)
                self._store(payload)
        except LockBusyError:
            return {"status": "error", "detail": "queue-busy"}
        chained_audit_log(
            2, "merge-queue enqueue", "governance/merge-queue", True,
            f"{entry['queue_id']} {branch}@{source_commit} priority={priority}",
            operation="merge-queue", phase="result", result="enqueued",
        )
        return entry

    # -- scheduling -----------------------------------------------------

    def next_entry(self) -> dict[str, Any] | None:
        """Highest priority, then FIFO; skips blocked/not-yet-due entries."""
        now = _now()
        payload = self._load()
        candidates = [
            item
            for item in payload["entries"]
            if item.get("status") == "pending"
            and float(item.get("not_before", 0.0)) <= now
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                -int(item.get("priority", 0)),
                int(item.get("enqueue_sequence", 0)),
            )
        )
        return candidates[0]

    def _transition(
        self,
        queue_id: str,
        status: str,
        *,
        detail: str = "",
        increment_attempt: bool = False,
    ) -> dict[str, Any] | None:
        if status not in QUEUE_STATUSES:
            return None
        with ProcessFileLock(self._lock_path):
            payload = self._load()
            entry = self._entry(payload["entries"], queue_id)
            if entry is None:
                return None
            entry["status"] = status
            entry["updated_at"] = _now()
            if detail:
                entry["blocked_reason" if status in {"blocked", "conflicted", "failed"} else "detail"] = detail
            if increment_attempt:
                entry["attempt_count"] = int(entry.get("attempt_count", 0)) + 1
            self._store(payload)
        chained_audit_log(
            2, "merge-queue transition", "governance/merge-queue", True,
            f"{queue_id} -> {status} {detail}"[:400],
            operation="merge-queue", phase="result", result=status,
        )
        return entry

    def mark_running(self, queue_id: str) -> dict[str, Any] | None:
        """Transition to running; refuses when the source SHA moved."""
        return self._transition(queue_id, "running", increment_attempt=True)

    def mark_merged(self, queue_id: str, detail: str = "") -> dict[str, Any] | None:
        return self._transition(queue_id, "merged", detail=detail)

    def mark_conflicted(self, queue_id: str, reason: str) -> dict[str, Any] | None:
        return self._transition(queue_id, "conflicted", detail=reason)

    def mark_blocked(self, queue_id: str, reason: str) -> dict[str, Any] | None:
        return self._transition(queue_id, "blocked", detail=reason)

    def mark_failed(self, queue_id: str, reason: str) -> dict[str, Any] | None:
        return self._transition(queue_id, "failed", detail=reason)

    def cancel(self, queue_id: str, reason: str = "") -> dict[str, Any] | None:
        return self._transition(queue_id, "cancelled", detail=reason)

    # -- staleness ------------------------------------------------------

    def is_stale(self, entry: dict[str, Any], current_sha: str) -> bool:
        """True when the branch HEAD moved after enqueue (re-enqueue needed)."""
        return bool(current_sha) and str(current_sha) != str(entry.get("source_commit", ""))

    def entries(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        payload = self._load()
        items = payload["entries"]
        if statuses is None:
            return list(items)
        wanted = set(statuses)
        return [item for item in items if item.get("status") in wanted]

    def find_entry(self, branch: str, source_commit: str) -> dict[str, Any] | None:
        """Latest entry for this exact branch+SHA binding, if any."""
        for item in reversed(self._load()["entries"]):
            if (
                item.get("source_branch") == branch
                and item.get("source_commit") == source_commit
            ):
                return item
        return None

    # -- health ---------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        now = _now()
        items = self._load()["entries"]
        pending = [i for i in items if i.get("status") == "pending"]
        oldest = min(
            (float(i.get("enqueue_time", now)) for i in pending), default=None
        )
        return {
            "depth": len(pending),
            "running": sum(1 for i in items if i.get("status") == "running"),
            "blocked": sum(1 for i in items if i.get("status") == "blocked"),
            "conflicted": sum(1 for i in items if i.get("status") == "conflicted"),
            "merged": sum(1 for i in items if i.get("status") == "merged"),
            "queue_age_seconds": (
                round(now - float(oldest), 1) if oldest is not None else 0.0
            ),
            "oldest_pending_age": (
                round(now - float(oldest), 1) if oldest is not None else 0.0
            ),
        }


__all__ = [
    "ESCALATION_ACTORS",
    "MergeQueue",
    "PRIORITY_NORMAL",
    "PRIORITY_URGENT",
    "QUEUE_STATUSES",
    "TERMINAL_STATUSES",
]
