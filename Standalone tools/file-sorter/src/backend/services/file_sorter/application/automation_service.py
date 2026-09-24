from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any


AUTOMATION_ENABLED_ENV = "GPTBRIDGE_FILE_SORTER_AUTOMATION_ENABLED"
POLL_INTERVAL_ENV = "GPTBRIDGE_FILE_SORTER_POLL_SECONDS"
DEFAULT_POLL_INTERVAL_SECONDS = 86_400.0
MIN_ENV_POLL_INTERVAL_SECONDS = 1.0
MAX_POLL_INTERVAL_SECONDS = 86_400.0
DEFAULT_REALTIME_SCAN_INTERVAL_SECONDS = 10.0
DEFAULT_SETTLE_RETRY_SECONDS = 0.5
DEFAULT_STARTUP_DELAY_SECONDS = 20.0
MAX_STARTUP_DELAY_SECONDS = 300.0
MAX_ADAPTIVE_SCAN_INTERVAL_SECONDS = 86_400.0
# Keyword edits run in a separate CLI subprocess (GovernedCliExecutor), so
# the loop learns about them through a state-root signal file. The file is
# polled at this bound so a keyword-added organize pass lands within 20 s
# even when the adaptive scan tier has backed off to its 24 h ceiling.
WAKE_SIGNAL_POLL_SECONDS = 20.0
WAKE_SIGNAL_NAME = "automation-wake.json"
WAKE_SIGNAL_CATEGORY = "signals"
ADAPTIVE_SCAN_INTERVAL_TIERS_SECONDS = (
    10.0,
    30.0,
    60.0,
    300.0,
    900.0,
    3_600.0,
    21_600.0,
    43_200.0,
    86_400.0,
)
# Directories skipped while snapshotting targets (same exclusions as the
# recursive duplicate scan; keeps VCS/cache noise out of the change watch).
_SNAPSHOT_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", "__pycache__", "_cleaner_backup", ".gptbridge_cleanerquarantine"}
)
# Snapshot cost bound: nested directories beyond this count are replaced by a
# stable truncation marker so an oversized tree cannot force rescan storms.
_MAX_SNAPSHOT_DIRECTORIES = 20_000


def _automation_enabled_from_environment() -> bool:
    value = os.environ.get(AUTOMATION_ENABLED_ENV)
    if value is None or not value.strip():
        return True
    return value.strip().casefold() not in {"0", "false", "no", "off", "disabled"}


def _poll_interval_from_environment() -> float:
    value = os.environ.get(POLL_INTERVAL_ENV)
    if value is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_SECONDS
    return min(
        MAX_POLL_INTERVAL_SECONDS,
        max(MIN_ENV_POLL_INTERVAL_SECONDS, interval),
    )


class FileSorterAutomationService:
    """Run enabled File Sorter profiles inside the persistent backend process.

    The sorter pass runs in a worker thread because file hashing, transaction
    recovery, and moves are synchronous. Stopping waits for an in-flight pass
    instead of abandoning a transaction while its worker thread is still active.
    """

    def __init__(
        self,
        project_root: str | Path,
        logger: Any | None = None,
        *,
        runner: Any | None = None,
        poll_interval: float | None = None,
        realtime_scan_interval: float = DEFAULT_REALTIME_SCAN_INTERVAL_SECONDS,
        settle_retry_seconds: float = DEFAULT_SETTLE_RETRY_SECONDS,
        startup_delay_seconds: float = DEFAULT_STARTUP_DELAY_SECONDS,
        wake_poll_seconds: float = WAKE_SIGNAL_POLL_SECONDS,
        state_root: str | Path | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.logger = logger
        self.enabled = (
            _automation_enabled_from_environment()
            if enabled is None
            else bool(enabled)
        )
        self.poll_interval = (
            _poll_interval_from_environment()
            if poll_interval is None
            else max(0.001, float(poll_interval))
        )
        self._runner = runner
        self.realtime_scan_interval = max(
            10.0,
            min(60.0, float(realtime_scan_interval)),
        )
        self.settle_retry_seconds = max(
            self.realtime_scan_interval,
            min(10.0, float(settle_retry_seconds)),
        )
        # First pass is deferred so tool startup finishes before any file
        # moves happen (boot services, UI wiring, governed channel attach).
        self.startup_delay_seconds = max(
            0.0,
            min(MAX_STARTUP_DELAY_SECONDS, float(startup_delay_seconds)),
        )
        self.wake_poll_seconds = max(
            1.0,
            min(300.0, float(wake_poll_seconds)),
        )
        self._state_root = state_root
        # mtime_ns of the last consumed wake signal; None = not yet seen
        # (a pre-existing file at startup is consumed without firing).
        self._wake_signal_mtime_ns: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._pass_lock = asyncio.Lock()
        self._pending_observation_count = 0
        self._unchanged_scan_count = 0
        self._last_error_type = ""
        self._last_pass_ok: bool | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Start one automation loop; repeated calls are idempotent."""

        async with self._lifecycle_lock:
            if not self.enabled:
                self._safe_log("info", "File Sorter automation is disabled.")
                return False
            if self._task is not None and not self._task.done():
                return False

            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(
                self._run_loop(self._stop_event),
                name="file-sorter-automation",
            )
            self._safe_log(
                "info",
                "File Sorter automation started.",
                {
                    "poll_interval_seconds": self.poll_interval,
                    "realtime_scan_interval_seconds": self.realtime_scan_interval,
                },
            )
            return True

    async def stop(self) -> None:
        """Signal the loop and safely wait for any active sorter pass."""

        async with self._lifecycle_lock:
            task = self._task
            stop_event = self._stop_event
            if task is None:
                return
            if stop_event is not None:
                stop_event.set()

        if task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                # External cancellation may already have ended the loop.
                pass

        async with self._lifecycle_lock:
            if self._task is task:
                self._task = None
                self._stop_event = None
        self._safe_log("info", "File Sorter automation stopped.")

    async def run_once(self) -> bool:
        """Run one non-reentrant recovery-and-sort pass."""

        if not self.enabled or self._pass_lock.locked():
            return False

        async with self._pass_lock:
            summary = await asyncio.to_thread(self._run_once_sync)
            self._last_pass_ok = summary.get("failed_profile_count", 0) == 0
            self._last_error_type = ""
            self._pending_observation_count = int(
                summary.get("waiting_for_second_observation_count", 0)
            )
        self._safe_log("info", "File Sorter automation pass completed.", summary)
        return True

    def _run_once_sync(self) -> dict[str, int]:
        runner = self._resolve_runner()
        recoveries = runner.recover_transactions()
        pruning = runner.prune_state()
        reports = runner.run_enabled_profiles_once()

        recovery_count = len(recoveries) if isinstance(recoveries, list) else 0
        profile_count = len(reports) if isinstance(reports, list) else 0
        moved_count = 0
        recycled_count = 0
        failed_profile_count = 0
        waiting_for_second_observation_count = 0
        if isinstance(reports, list):
            for report in reports:
                if not isinstance(report, dict):
                    continue
                moved = report.get("moved_count", 0)
                if isinstance(moved, int) and not isinstance(moved, bool):
                    moved_count += max(0, moved)
                recycled = report.get("recycled_count", 0)
                if isinstance(recycled, int) and not isinstance(recycled, bool):
                    recycled_count += max(0, recycled)
                if report.get("ok") is False:
                    failed_profile_count += 1
                waiting = report.get("waiting_for_second_observation_count", 0)
                if isinstance(waiting, int) and not isinstance(waiting, bool):
                    waiting_for_second_observation_count += max(0, waiting)
                duplicate_waiting = report.get(
                    "duplicate_waiting_for_second_observation_count",
                    0,
                )
                if isinstance(duplicate_waiting, int) and not isinstance(
                    duplicate_waiting,
                    bool,
                ):
                    waiting_for_second_observation_count += max(0, duplicate_waiting)

        return {
            "recovery_count": recovery_count,
            "removed_expired_plan_count": int(pruning.get("removed_plans", 0)),
            "removed_old_journal_count": int(pruning.get("removed_journals", 0)),
            "removed_old_recycle_journal_count": int(
                pruning.get("removed_recycle_journals", 0)
            ),
            "profile_count": profile_count,
            "moved_count": moved_count,
            "recycled_count": recycled_count,
            "failed_profile_count": failed_profile_count,
            "waiting_for_second_observation_count": (
                waiting_for_second_observation_count
            ),
        }

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        if self.startup_delay_seconds > 0:
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.startup_delay_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
        # A signal file left behind by a previous process is consumed at
        # startup without firing — only signals written while this loop is
        # alive trigger an early pass.
        self._mark_wake_signal_seen()
        while not stop_event.is_set():
            try:
                before_pass = await asyncio.to_thread(
                    self._snapshot_enabled_targets
                )
                await self.run_once()
                if stop_event.is_set():
                    break
                after_pass = await asyncio.to_thread(
                    self._snapshot_enabled_targets
                )
                if after_pass != before_pass:
                    # A file appeared or changed during the pass. Rescan now.
                    continue
                await self._wait_for_file_change(stop_event, after_pass)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Keep the owner loop alive. Exception messages may contain
                # paths or credentials, so expose only the stable type.
                self._last_pass_ok = False
                self._last_error_type = type(error).__name__
                self._safe_log(
                    "error",
                    "File Sorter automation cycle failed; it will retry.",
                    {"error_type": self._last_error_type},
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "automation_enabled": self.enabled,
            "automation_running": self.running,
            "automation_last_pass_ok": self._last_pass_ok,
            "automation_last_error_type": self._last_error_type,
            "automation_pending_observations": self._pending_observation_count,
            "automation_startup_delay_seconds": self.startup_delay_seconds,
            "automation_wake_poll_seconds": self.wake_poll_seconds,
        }

    async def _wait_for_file_change(
        self,
        stop_event: asyncio.Event,
        baseline: dict[str, tuple[tuple[str, str, int, int], ...]],
    ) -> None:
        """Wake on a target change, settle retry, or fallback poll."""

        fallback_deadline = time.monotonic() + self.poll_interval
        settle_deadline = (
            time.monotonic() + self.settle_retry_seconds
            if self._pending_observation_count
            else None
        )
        scan_due = time.monotonic() + self._adaptive_scan_interval(
            baseline, settling=settle_deadline is not None
        )
        while not stop_event.is_set():
            now = time.monotonic()
            deadlines = [fallback_deadline, scan_due, now + self.wake_poll_seconds]
            if settle_deadline is not None:
                deadlines.append(settle_deadline)
            timeout = max(0.0, min(deadlines) - now)
            if timeout:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                    return
                except asyncio.TimeoutError:
                    pass
            now = time.monotonic()
            if self._consume_wake_signal():
                return
            if now >= fallback_deadline:
                return
            if settle_deadline is not None and now >= settle_deadline:
                return
            if now >= scan_due:
                current = await asyncio.to_thread(self._snapshot_enabled_targets)
                if current != baseline:
                    self._unchanged_scan_count = 0
                    return
                self._unchanged_scan_count += 1
                scan_due = now + self._adaptive_scan_interval(
                    baseline,
                    settling=settle_deadline is not None,
                )

    def _wake_signal_path(self) -> Path | None:
        """Validated wake-signal document under the tool-owned state root."""

        try:
            from ..infrastructure.sorter_engine import (
                _validated_state_document_path,
                resolve_state_root,
            )

            path = (
                resolve_state_root(self._state_root)
                / WAKE_SIGNAL_CATEGORY
                / WAKE_SIGNAL_NAME
            )
            return _validated_state_document_path(
                path,
                state_root=self._state_root,
                category=WAKE_SIGNAL_CATEGORY,
                relative_parts=1,
                require_exists=False,
            )
        except Exception:
            return None

    def _mark_wake_signal_seen(self) -> None:
        path = self._wake_signal_path()
        if path is None:
            return
        try:
            self._wake_signal_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            self._wake_signal_mtime_ns = None

    def _consume_wake_signal(self) -> bool:
        """True when a CLI writer left a new wake signal since last check."""

        path = self._wake_signal_path()
        if path is None:
            return False
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return False
        if self._wake_signal_mtime_ns is None:
            # First observation inside a running loop still fires — a
            # pre-startup file was already consumed by _mark_wake_signal_seen.
            self._wake_signal_mtime_ns = mtime_ns
            return True
        if mtime_ns != self._wake_signal_mtime_ns:
            self._wake_signal_mtime_ns = mtime_ns
            return True
        return False

    def _adaptive_scan_interval(
        self,
        baseline: dict[str, tuple[tuple[str, str, int, int], ...]],
        *,
        settling: bool,
    ) -> float:
        """Choose a light-weight polling cadence from activity and folder size."""

        if settling:
            return self.realtime_scan_interval

        file_count = sum(len(entries) for entries in baseline.values())
        size_tier_offset = 2 if file_count >= 10_000 else 1 if file_count >= 1_000 else 0
        unchanged_tier = self._unchanged_scan_count // 2
        # Every target starts at the fastest tier. Folder size only accelerates
        # backing off after two unchanged observations have established idleness.
        tier_index = 0 if unchanged_tier == 0 else unchanged_tier + size_tier_offset
        tier_index = min(tier_index, len(ADAPTIVE_SCAN_INTERVAL_TIERS_SECONDS) - 1)
        tier_interval = ADAPTIVE_SCAN_INTERVAL_TIERS_SECONDS[tier_index]
        return min(
            self.poll_interval,
            MAX_ADAPTIVE_SCAN_INTERVAL_SECONDS,
            max(10.0, self.realtime_scan_interval, tier_interval),
        )

    def _snapshot_enabled_targets(
        self,
    ) -> dict[str, tuple[tuple[str, str, int, int], ...]]:
        """Build a metadata-only snapshot without following directory links.

        Top-level files are recorded as ``("f", name, size, mtime_ns)`` — the
        classification scope. Directories are recorded recursively as
        ``("d", relative_path, 0, mtime_ns)`` so adds/removes under nested
        folders (the duplicate-recycle scope) also wake the loop instead of
        waiting for the daily fallback poll. File content rewrites that keep
        the same name do not change a parent directory's mtime; those still
        surface through the fallback poll and remain protected by two-pass
        observation before any recycle.
        """

        from ..infrastructure.cleanup_utils import _is_link_or_reparse

        runner = self._resolve_runner()
        target_provider = getattr(runner, "enabled_profile_targets", None)
        if not callable(target_provider):
            return {}
        try:
            raw_targets = target_provider()
        except Exception:
            return {}
        if not isinstance(raw_targets, (list, tuple, set)):
            return {}

        snapshots: dict[str, tuple[tuple[str, str, int, int], ...]] = {}
        for raw_target in raw_targets:
            try:
                target = Path(raw_target).resolve(strict=True)
                if not target.is_dir() or target.is_symlink():
                    continue
                entries: list[tuple[str, str, int, int]] = []
                directories_seen = 0
                truncated = False
                for current_root, dir_names, file_names in os.walk(
                    target, topdown=True, followlinks=False
                ):
                    current = Path(current_root)
                    kept_dirs: list[str] = []
                    for name in dir_names:
                        if name in _SNAPSHOT_EXCLUDED_DIRECTORY_NAMES:
                            continue
                        if _is_link_or_reparse(current / name):
                            continue
                        kept_dirs.append(name)
                    dir_names[:] = kept_dirs
                    if current == target:
                        for name in file_names:
                            file_path = current / name
                            try:
                                if (
                                    not file_path.is_file()
                                    or file_path.is_symlink()
                                ):
                                    continue
                                value = file_path.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            entries.append(
                                (
                                    "f",
                                    name,
                                    int(value.st_size),
                                    int(value.st_mtime_ns),
                                )
                            )
                    for name in dir_names:
                        candidate = current / name
                        try:
                            value = candidate.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        entries.append(
                            (
                                "d",
                                str(candidate.relative_to(target)),
                                0,
                                int(value.st_mtime_ns),
                            )
                        )
                        directories_seen += 1
                        if directories_seen >= _MAX_SNAPSHOT_DIRECTORIES:
                            truncated = True
                    if truncated:
                        entries.append(("d", ".truncated", -1, 0))
                        break
                snapshots[str(target)] = tuple(sorted(entries))
            except (OSError, RuntimeError, ValueError):
                continue
        return snapshots

    def _resolve_runner(self) -> Any:
        if self._runner is not None:
            return self._runner
        from . import cli_organize

        for required_name in (
            "prune_state",
            "recover_transactions",
            "run_enabled_profiles_once",
        ):
            if not callable(getattr(cli_organize, required_name, None)):
                raise ImportError(f"File Sorter runtime is missing {required_name}().")
        self._runner = cli_organize
        return cli_organize

    def _safe_log(
        self,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.logger is None:
            return
        safe_payload = {"level": level}
        if payload:
            safe_payload.update(payload)
        try:
            write = getattr(self.logger, "write", None)
            if callable(write):
                write("file-sorter", message, safe_payload)
                return
            method = getattr(self.logger, level, None)
            if callable(method):
                method(message, safe_payload)
        except Exception:
            # Logging must never stop or duplicate a filesystem transaction.
            return
