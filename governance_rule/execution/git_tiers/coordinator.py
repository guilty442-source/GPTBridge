"""Git Coordinator — multi-AI worktree orchestration (A53/E39).

Architecture:

                Git Coordinator
                       |
        +--------------+--------------+
        |              |              |
      AI-1           AI-2           AI-3
        |              |              |
     worktree       worktree       worktree
        |              |              |
     branch A       branch B       branch C
        +--------------+--------------+
                       |
                  git-gate
                       |
                  git_tiers
                       |
               Local Bare Repo
                       |
                 Merge Queue
                       |
                     main

The coordinator manages:
  1. Worktree registration — each AI worker gets an isolated worktree + branch.
  2. Merge queue — serialized merges into main to avoid conflicts.
  3. Gate enforcement — every operation passes through git-gate / git_tiers.
  4. Recovery — on failure, the audit ledger's head_revision enables rollback.

Design principles:
  - Model core stays independent of network (governance boundary).
  - Only one merge into main at a time (serial merge queue).
  - Every worktree operation is audited with pre-operation snapshot.
  - Tier-3 operations (force-push, history rewrite) require authority approval.

Prerequisite — central bare repo setup:
  The merge flow pushes to a ``central`` remote that points to a local bare
  repository at ``GPTBridge.git`` (sibling of the project root).  This bare
  repo MUST be initialized before execute_merge() is called:

      git init --bare GPTBridge.git
      git remote add central /path/to/GPTBridge.git

  Without this remote, execute_merge() will fail at the push step with a
  "central push failed" detail.  The bare repo acts as the serialization
  point: all worker branches merge into main locally, then push to central
  to publish the integrated history.  This keeps the workflow local-only
  (A44: local-governed; A58: no unmanaged network).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .git_repository import GitRepository
from .worktree_manager import WorktreeManager

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKTREE_ROOT = PROJECT_ROOT.parent / "GPTBridge-worktrees"
BARE_REPO = PROJECT_ROOT.parent / "GPTBridge.git"
AUDIT_LEDGER = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "git_tier_audit.jsonl"
MERGE_QUEUE_LEDGER = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "merge_queue.jsonl"
MERGE_QUEUE_LOCK = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "merge_queue.lock"


@contextmanager
def _serial_merge_lock() -> Iterator[None]:
    MERGE_QUEUE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with MERGE_QUEUE_LOCK.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("GIT_MERGE_QUEUE_BUSY") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("GIT_MERGE_QUEUE_BUSY") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class WorktreeSlot:
    """A registered AI worker slot."""

    worker_id: str
    worktree_path: Path
    branch: str
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))

    @property
    def exists(self) -> bool:
        return self.worktree_path.is_dir() and (self.worktree_path / ".git").exists()

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worktree_path": str(self.worktree_path),
            "branch": self.branch,
            "created_at": self.created_at,
        }


class MergeQueueEntry:
    """A single entry in the serial merge queue."""

    def __init__(
        self,
        worker_id: str,
        branch: str,
        operation: str = "merge",
        status: str = "pending",
        detail: str = "",
    ) -> None:
        self.worker_id = worker_id
        self.branch = branch
        self.operation = operation
        self.status = status  # pending → running → merged / failed / aborted
        self.detail = detail
        self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "worker_id": self.worker_id,
            "branch": self.branch,
            "operation": self.operation,
            "status": self.status,
            "detail": self.detail,
        }

    def write(self) -> None:
        MERGE_QUEUE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(MERGE_QUEUE_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(self.to_dict(), ensure_ascii=False) + "\n")


class GitCoordinator:
    """Coordinates multiple AI workers each operating in isolated worktrees.

    Usage:
        coord = GitCoordinator()
        slot = coord.register_worker("ai-1", branch="feature/ai-1")
        coord.enqueue_merge(slot)  # enters the serial merge queue
    """

    def __init__(self, *, actor: str = "governance/git-coordinator") -> None:
        self._actor = actor
        self._repo = GitRepository(PROJECT_ROOT)
        self._wt_manager = WorktreeManager(self._repo, actor=actor)
        self._slots: dict[str, WorktreeSlot] = {}
        self._refresh_slots()

    def _refresh_slots(self) -> None:
        """Read existing worktrees via WorktreeManager."""
        for wt in self._wt_manager.list_worktrees():
            path_str = wt.get("path", "")
            if not path_str or Path(path_str) == PROJECT_ROOT:
                continue  # skip main worktree — it's the merge target
            wt_path = Path(path_str)
            worker_id = wt_path.name
            branch = wt.get("branch", "HEAD")
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            if worker_id and wt_path.exists():
                self._slots[worker_id] = WorktreeSlot(
                    worker_id=worker_id,
                    worktree_path=wt_path,
                    branch=branch or "main",
                )

    @property
    def slots(self) -> dict[str, WorktreeSlot]:
        return dict(self._slots)

    def list_workers(self) -> list[WorktreeSlot]:
        return list(self._slots.values())

    def register_worker(
        self,
        worker_id: str,
        branch: str | None = None,
        *,
        confirmed: bool | None = None,
    ) -> WorktreeSlot:
        """Register a new AI worker with an isolated worktree.

        Creates the worktree at GPTBridge-worktrees/<worker_id> on a new
        branch from main. If the worktree already exists, returns the
        existing slot.
        """
        if worker_id in self._slots and self._slots[worker_id].exists:
            return self._slots[worker_id]

        branch = branch or f"feature/{worker_id}"
        wt_path = WORKTREE_ROOT / worker_id

        # Create worktree from main via WorktreeManager
        if not self._wt_manager.create(str(wt_path), branch, confirmed=confirmed):
            if not wt_path.exists():
                raise RuntimeError(
                    f"failed to create worktree for {worker_id}"
                )

        slot = WorktreeSlot(
            worker_id=worker_id,
            worktree_path=wt_path,
            branch=branch,
        )
        self._slots[worker_id] = slot
        return slot

    def remove_worker(
        self,
        worker_id: str,
        *,
        confirmed: bool | None = None,
        authority_approved: bool | None = None,
    ) -> bool:
        """Remove an AI worker only after Tier-2 and Tier-3 authorization."""
        slot = self._slots.get(worker_id)
        if slot is None:
            return False
        self._repo.run(
            ["worktree", "remove", str(slot.worktree_path), "--force"],
            confirmed=confirmed,
            actor=self._actor,
        )
        self._repo.run(
            ["branch", "-D", slot.branch],
            authority_approved=authority_approved,
            actor=self._actor,
        )
        self._slots.pop(worker_id, None)
        return True

    def enqueue_merge(
        self,
        slot: WorktreeSlot,
        *,
        target_branch: str = "integration",
    ) -> MergeQueueEntry:
        """Enqueue a merge from the worker's branch into target (main).

        The merge is serialized — only one merge runs at a time. The
        operation passes through git-gate (Tier-2) and is audited with
        pre-operation snapshot for recovery.
        """
        entry = MergeQueueEntry(
            worker_id=slot.worker_id,
            branch=slot.branch,
            operation="merge",
            status="pending",
        )
        entry.write()
        return entry

    def execute_merge(
        self,
        slot: WorktreeSlot,
        *,
        target: str = "integration",
        confirmed: bool | None = None,
        authority_approved: bool | None = None,
    ) -> MergeQueueEntry:
        """Execute one validated merge while holding the cross-process queue lock."""
        from governance_rule.execution.git_tiers.snapshot import _capture_repo_snapshot

        entry = MergeQueueEntry(
            worker_id=slot.worker_id,
            branch=slot.branch,
            operation="merge",
            status="pending",
        )
        entry.write()
        if target == "main" and not (
            authority_approved or os.environ.get("GOVERNANCE_AUTHORITY_APPROVAL") == "1"
        ):
            entry.status = "failed"
            entry.detail = "merge to main requires authority approval"
            entry.write()
            return entry
        if not slot.exists or not slot.branch or slot.branch.startswith("-"):
            entry.status = "failed"
            entry.detail = "worker slot or source branch is invalid"
            entry.write()
            return entry
        try:
            with _serial_merge_lock():
                snapshot = _capture_repo_snapshot(PROJECT_ROOT)
                entry.status = "running"
                entry.write()
                if self._repo.current_branch() != target:
                    entry.status = "failed"
                    entry.detail = f"target worktree is not on {target}"
                    entry.write()
                    return entry
                if self._repo.status():
                    entry.status = "failed"
                    entry.detail = "target worktree is not clean"
                    entry.write()
                    return entry
                central_ancestor = self._repo.run(
                    ["merge-base", "--is-ancestor", f"central/{target}", target],
                    actor=self._actor,
                )
                if central_ancestor.returncode != 0:
                    entry.status = "failed"
                    entry.detail = "central target is not an ancestor of local target"
                    entry.write()
                    return entry
                source_revision = GitRepository(slot.worktree_path).head()
                fetch_result = self._repo.run(
                    ["fetch", str(slot.worktree_path), f"{slot.branch}:{slot.branch}"],
                    confirmed=confirmed,
                    actor=self._actor,
                )
                if fetch_result.returncode != 0:
                    entry.status = "failed"
                    entry.detail = f"fetch failed: {fetch_result.stderr.strip()[:500]}"
                    entry.write()
                    return entry
                fetched_revision = self._repo.run(
                    ["rev-parse", slot.branch],
                    actor=self._actor,
                ).stdout.strip()
                if fetched_revision != source_revision:
                    entry.status = "failed"
                    entry.detail = "source branch changed during merge preparation"
                    entry.write()
                    return entry
                merge_result = self._repo.run(
                    [
                        "merge", "--no-ff", slot.branch,
                        "-m", f"merge: {slot.worker_id}/{slot.branch} into {target}",
                    ],
                    confirmed=confirmed,
                    actor=self._actor,
                )
                if merge_result.returncode != 0:
                    self._repo.run(
                        ["merge", "--abort"],
                        confirmed=True,
                        actor=self._actor,
                    )
                    entry.status = "failed"
                    entry.detail = f"merge failed; recovery target: {snapshot['head_revision']}"
                    entry.write()
                    return entry
                push_result = self._repo.run(
                    ["push", "central", target],
                    confirmed=confirmed,
                    actor=self._actor,
                )
                if push_result.returncode != 0:
                    entry.status = "failed"
                    entry.detail = (
                        f"central push failed; local merge retained; recovery target: "
                        f"{snapshot['head_revision']}"
                    )
                    entry.write()
                    return entry
                entry.status = "merged"
                entry.detail = (
                    f"merged {slot.branch}@{source_revision} into {target}; "
                    f"previous target: {snapshot['head_revision']}"
                )
                entry.write()
                return entry
        except (OSError, PermissionError, RuntimeError) as exc:
            entry.status = "failed"
            entry.detail = str(exc)[:500]
            entry.write()
            return entry

    def status(self) -> dict[str, Any]:
        """Return coordinator status: workers, worktrees, queue length."""
        return {
            "workers": [s.to_dict() for s in self._slots.values()],
            "worktree_root": str(WORKTREE_ROOT),
            "bare_repo": str(BARE_REPO),
            "merge_queue_ledger": str(MERGE_QUEUE_LEDGER),
        }


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry: git-coordinator <subcommand> [args]

    Subcommands:
      list       — list registered workers
      status     — show coordinator status
      register <worker-id> [branch]  — register a new worker
      remove <worker-id>             — remove a worker
      merge <worker-id>              — enqueue + execute merge
    """
    args = argv if argv is not None else __import__("sys").argv[1:]
    if not args:
        print("usage: git-coordinator <list|status|register|remove|merge>", file=__import__("sys").stderr)
        return 1

    coord = GitCoordinator()
    cmd = args[0]

    if cmd == "list":
        for slot in coord.list_workers():
            print(f"  {slot.worker_id:20s}  {slot.branch:30s}  {slot.worktree_path}")
        return 0

    if cmd == "status":
        print(json.dumps(coord.status(), indent=2, ensure_ascii=False))
        return 0

    if cmd == "register":
        if len(args) < 2:
            print("usage: git-coordinator register <worker-id> [branch]", file=__import__("sys").stderr)
            return 1
        worker_id = args[1]
        branch = args[2] if len(args) > 2 else None
        slot = coord.register_worker(worker_id, branch)
        print(f"registered: {slot.worker_id} -> {slot.worktree_path} (branch: {slot.branch})")
        return 0

    if cmd == "remove":
        if len(args) < 2:
            print("usage: git-coordinator remove <worker-id>", file=__import__("sys").stderr)
            return 1
        if coord.remove_worker(args[1]):
            print(f"removed: {args[1]}")
            return 0
        print(f"not found: {args[1]}", file=__import__("sys").stderr)
        return 1

    if cmd == "merge":
        if len(args) < 2:
            print("usage: git-coordinator merge <worker-id>", file=__import__("sys").stderr)
            return 1
        slot = coord._slots.get(args[1])
        if slot is None:
            print(f"worker not found: {args[1]}", file=__import__("sys").stderr)
            return 1
        entry = coord.execute_merge(slot)
        print(f"merge {entry.status}: {entry.detail}")
        return 0 if entry.status == "merged" else 1

    print(f"unknown subcommand: {cmd}", file=__import__("sys").stderr)
    return 1
