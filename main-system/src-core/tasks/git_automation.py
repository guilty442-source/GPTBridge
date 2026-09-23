"""Git worktree automation as a main-system task (replaces the watcher fleet).

One in-process asyncio loop replaces the old ``automation_supervisor``
process fleet: instead of spawning one self-commit watcher per worktree
plus a periodic sync process, a single task runs two governed operations
on a schedule:

- **commit sweep** (``sweep_interval``): for every registered worktree,
  run ``self_commit.run_once`` — but only after the dirty fingerprint has
  been stable for ``debounce_seconds`` (same stability contract the old
  per-worktree watchers provided, without one process per worktree).
- **sync cycle** (``sync_interval``): run
  ``workspace_sync.synchronize`` — commit, merge worker branches into
  ``main``, audit, fast-forward clean worktrees.  Conflicts stop that
  cycle, exactly as before.

All governance guarantees are unchanged: the underlying functions own
locking, merge/rebase guards, audit recording, and never push unless
asked.  This task adds scheduling and observability only.

State is written to ``main-system/runtime/state/git-automation.json``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.git_automation")

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "git-automation.json"
)

_DEFAULT_SWEEP_INTERVAL = 60.0
_DEFAULT_SYNC_INTERVAL = 300.0
_DEFAULT_DEBOUNCE_SECONDS = 60.0


class GitAutomationService:
    """Periodic self-commit sweep + workspace sync, one process total."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        sweep_interval: float = _DEFAULT_SWEEP_INTERVAL,
        sync_interval: float = _DEFAULT_SYNC_INTERVAL,
        debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
        push: bool = False,
        scheduler: Any | None = None,
        automation_core: Any | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.sweep_interval = max(10.0, float(sweep_interval))
        self.sync_interval = max(self.sweep_interval, float(sync_interval))
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.push = bool(push)

        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._dirty_since: dict[str, tuple[str, float]] = {}
        self._next_sync_at = 0.0
        self._queue_file_path: Path | None = None
        self._last_sweep: dict[str, Any] = {}
        self._last_sync: dict[str, Any] = {}
        self._sweeps = 0
        self._syncs = 0
        # §10.63 R3: shared PeriodicScheduler rides this service's sweep
        # cadence instead of a private task (due gates unchanged).
        # §1.1 自動化集中：when present the automation core is the single
        # registration point (allowlist + unified audit + kill switch);
        # denial must not fall back to a private loop.
        self._scheduler = scheduler
        self._automation_core = automation_core

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        if not (self.project_root / ".git").exists():
            return {"status": "skipped", "reason": "not-a-git-worktree"}
        self._stop_event.clear()
        if self._automation_core is not None:
            if self._automation_core.register_flow(
                "git-automation",
                self._cycle_tick,
                interval_s=self.sweep_interval,
                run_immediately=True,
            ):
                _logger.info(
                    "git automation started via automation core "
                    "(sweep=%.0fs sync=%.0fs debounce=%.0fs)",
                    self.sweep_interval, self.sync_interval,
                    self.debounce_seconds,
                )
                return {"status": "started", "loop": "automation-core"}
            _logger.info("git automation disabled by automation core")
            return {"status": "disabled", "loop": "automation-core"}
        if self._scheduler is not None:
            self._scheduler.register(
                "git-automation", self.sweep_interval, self._cycle_tick,
                run_immediately=True, pausable=True,
            )
            _logger.info(
                "git automation started on periodic scheduler "
                "(sweep=%.0fs sync=%.0fs debounce=%.0fs)",
                self.sweep_interval, self.sync_interval, self.debounce_seconds,
            )
            return {"status": "started", "loop": "periodic-scheduler"}
        try:
            self._task = asyncio.create_task(
                self._loop(), name="git-automation"
            )
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        _logger.info(
            "git automation started (sweep=%.0fs sync=%.0fs debounce=%.0fs)",
            self.sweep_interval, self.sync_interval, self.debounce_seconds,
        )
        return {"status": "started"}

    async def stop(self) -> None:
        self._stop_event.set()
        if self._automation_core is not None:
            self._automation_core.unregister("git-automation")
        elif self._scheduler is not None:
            self._scheduler.unregister("git-automation")
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # -- loop -----------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cycle_tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # never kill the loop
                _logger.warning("git automation cycle error: %s", error)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.sweep_interval
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _cycle_tick(self) -> None:
        """One sweep + due-gated sync — shared by the private loop and the
        PeriodicScheduler job (§10.63 R3).  G101/§3.3: a pending merge-queue
        entry is a queue event — it runs the sync early instead of waiting
        for the fixed interval."""
        if self._stop_event.is_set():
            return
        await self.run_sweep()
        now = time.monotonic()
        queue_event = await asyncio.to_thread(self._queue_has_pending)
        if queue_event or now >= self._next_sync_at:
            await self.run_sync()
            self._next_sync_at = now + self.sync_interval

    def _queue_file(self) -> Path | None:
        """Merge-queue file in the repo-common dir (resolved once — the
        layout never changes for a service instance)."""
        if self._queue_file_path is not None:
            return self._queue_file_path
        from governance_rule.execution.git_tiers.git_repository import (
            GitRepository,
        )

        try:
            repo = GitRepository(self.project_root)
            raw = (
                repo.run(["rev-parse", "--git-common-dir"]).stdout or ""
            ).strip()
            common = Path(raw)
            if not common.is_absolute():
                common = repo.path / common
            self._queue_file_path = (
                common.resolve()
                / "gptbridge-automation"
                / "merge-queue"
                / "queue.json"
            )
        except Exception:
            self._queue_file_path = None
        return self._queue_file_path

    def _queue_has_pending(self) -> bool:
        """True when the merge queue holds a pending entry (queue event).

        The queue file lives in the repo-common dir so a cheap read per
        tick detects cross-process enqueues without parsing every cycle.
        """
        queue_file = self._queue_file()
        try:
            payload = json.loads(queue_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        entries = payload.get("entries") if isinstance(payload, dict) else []
        return any(
            isinstance(e, dict) and e.get("status") == "pending"
            for e in entries or []
        )

    # -- operations -----------------------------------------------------

    def _list_worktrees(self) -> list[str]:
        from governance_rule.execution.git_tiers.git_repository import (
            GitRepository,
        )
        from governance_rule.execution.git_tiers.worktree_manager import (
            WorktreeManager,
        )

        manager = WorktreeManager(GitRepository(self.project_root))
        paths = [
            str(item["path"])
            for item in manager.list_worktrees()
            if item.get("path")
        ]
        main = str(self.project_root)
        normalized = {os.path.normcase(p) for p in paths}
        if os.path.normcase(main) not in normalized:
            paths.insert(0, main)
        return paths

    def _worktree_snapshot(self, worktree: str):
        """Shared per-generation snapshot (G101): one porcelain capture
        per sweep tick, reused by the debounce check and ``run_once``;
        the TTL equals the sweep cadence so the sweep stays the
        low-frequency insurance of §3.3."""
        from governance_rule.execution.git_tiers.generation_snapshot import (
            generation_snapshot,
        )

        try:
            return generation_snapshot(
                worktree, max_age_s=self.sweep_interval
            )
        except Exception:
            return None

    async def run_sweep(self) -> dict[str, Any]:
        """One self-commit sweep across all worktrees (debounced)."""
        from governance_rule.execution.git_tiers.self_commit import run_once

        results: dict[str, str] = {}
        scopes: dict[str, list[str]] = {}
        now = time.monotonic()
        for worktree in await asyncio.to_thread(self._list_worktrees):
            snapshot = await asyncio.to_thread(
                self._worktree_snapshot, worktree
            )
            fingerprint = snapshot.fingerprint if snapshot else ""
            if not fingerprint:
                self._dirty_since.pop(worktree, None)
                continue
            scopes[worktree] = list(snapshot.affected_scope)
            marker = self._dirty_since.get(worktree)
            if marker is None or marker[0] != fingerprint:
                self._dirty_since[worktree] = (fingerprint, now)
                results[worktree] = "debounce"
                continue
            if now - marker[1] < self.debounce_seconds:
                results[worktree] = "debounce"
                continue
            status = await asyncio.to_thread(
                run_once, worktree, snapshot=snapshot
            )
            results[worktree] = status
            if status in ("committed", "clean"):
                self._dirty_since.pop(worktree, None)
        self._sweeps += 1
        self._last_sweep = {
            "at": time.time(),
            "results": results,
            "affected_scope": scopes,
        }
        self._write_state()
        return self._last_sweep

    async def run_sync(self) -> dict[str, Any]:
        """One workspace synchronization cycle (commit→merge→ff)."""
        from governance_rule.execution.git_tiers.workspace_sync import (
            synchronize,
        )

        # §10.69-E① coordinator push: governed by the automation-flows
        # manifest (``flows.git-automation.push``) when the core is the
        # registration point; the constructor flag is the fallback.
        push = self.push
        if self._automation_core is not None:
            entry = self._automation_core.flow_entry("git-automation") or {}
            push = bool(entry.get("push", push))
        try:
            result = await asyncio.to_thread(
                synchronize,
                self.project_root,
                commit_dirty=True,
                push=push,
            )
        except Exception as exc:
            # Lock-busy is routine serialization (a manual or concurrent
            # sync holds gptbridge-workspace-sync.lock): skip this cycle
            # as a normal status instead of failing the flow — an error
            # log per collision poisons log-hygiene budgets (INT-10).
            from governance_rule.execution.git_tiers.process_lock import (
                LockBusyError,
            )

            if not isinstance(exc, LockBusyError):
                raise
            result = "skipped:lock-busy"
        self._syncs += 1
        self._last_sync = {"at": time.time(), "result": result}
        self._write_state()
        return self._last_sync

    async def run_once_cycle(self) -> dict[str, Any]:
        """One-shot sweep + sync (for manual runs and verification)."""
        sweep = await self.run_sweep()
        sync = await self.run_sync()
        return {"sweep": sweep, "sync": sync}

    # -- observability --------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task is not None and not self._task.done()),
            "project_root": str(self.project_root),
            "sweep_interval": self.sweep_interval,
            "sync_interval": self.sync_interval,
            "debounce_seconds": self.debounce_seconds,
            "push": self.push,
            "sweeps": self._sweeps,
            "syncs": self._syncs,
            "pending_debounce": sorted(self._dirty_since),
            "last_sweep": self._last_sweep,
            "last_sync": self._last_sync,
        }

    def _write_state(self) -> None:
        payload = {"updated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        payload.update(self.status())
        temporary = _STATE_FILE.with_name(
            _STATE_FILE.name + f".{os.getpid()}.tmp"
        )
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, _STATE_FILE)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = ["GitAutomationService"]
