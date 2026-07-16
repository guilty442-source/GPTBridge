from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any


AUTOMATION_ENABLED_ENV = "GPTBRIDGE_FILE_SORTER_AUTOMATION_ENABLED"
POLL_INTERVAL_ENV = "GPTBRIDGE_FILE_SORTER_POLL_SECONDS"
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
MIN_ENV_POLL_INTERVAL_SECONDS = 1.0
MAX_POLL_INTERVAL_SECONDS = 3600.0
DEFAULT_REALTIME_SCAN_INTERVAL_SECONDS = 0.25
DEFAULT_SETTLE_RETRY_SECONDS = 0.5


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
            0.05,
            min(5.0, float(realtime_scan_interval)),
        )
        self.settle_retry_seconds = max(
            self.realtime_scan_interval,
            min(10.0, float(settle_retry_seconds)),
        )
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._pass_lock = asyncio.Lock()
        self._pending_observation_count = 0

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
            self._pending_observation_count = int(
                summary.get("waiting_for_second_observation_count", 0)
            )
        self._safe_log("info", "File Sorter automation pass completed.", summary)
        return True

    def _run_once_sync(self) -> dict[str, int]:
        runner = self._resolve_runner()
        recoveries = runner.recover_transactions()
        reports = runner.run_enabled_profiles_once()

        recovery_count = len(recoveries) if isinstance(recoveries, list) else 0
        profile_count = len(reports) if isinstance(reports, list) else 0
        moved_count = 0
        failed_profile_count = 0
        waiting_for_second_observation_count = 0
        if isinstance(reports, list):
            for report in reports:
                if not isinstance(report, dict):
                    continue
                moved = report.get("moved_count", 0)
                if isinstance(moved, int) and not isinstance(moved, bool):
                    moved_count += max(0, moved)
                if report.get("ok") is False:
                    failed_profile_count += 1
                waiting = report.get("waiting_for_second_observation_count", 0)
                if isinstance(waiting, int) and not isinstance(waiting, bool):
                    waiting_for_second_observation_count += max(0, waiting)

        return {
            "recovery_count": recovery_count,
            "profile_count": profile_count,
            "moved_count": moved_count,
            "failed_profile_count": failed_profile_count,
            "waiting_for_second_observation_count": (
                waiting_for_second_observation_count
            ),
        }

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            before_pass = await asyncio.to_thread(
                self._snapshot_enabled_targets
            )
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Do not include exception messages: paths, filenames, or
                # provider credentials can appear in third-party exceptions.
                self._safe_log(
                    "error",
                    "File Sorter automation pass failed; it will retry.",
                    {"error_type": type(error).__name__},
                )

            if stop_event.is_set():
                break
            after_pass = await asyncio.to_thread(
                self._snapshot_enabled_targets
            )
            if after_pass != before_pass:
                # A file appeared or changed while the sorter pass was running.
                # Run again immediately so the event cannot fall into the
                # small gap between a pass and watcher baseline creation.
                continue
            await self._wait_for_file_change(stop_event, after_pass)

    async def _wait_for_file_change(
        self,
        stop_event: asyncio.Event,
        baseline: dict[str, tuple[tuple[str, int, int], ...]],
    ) -> None:
        """Wake on a top-level file change, settle retry, or fallback poll."""

        fallback_deadline = time.monotonic() + self.poll_interval
        settle_deadline = (
            time.monotonic() + self.settle_retry_seconds
            if self._pending_observation_count
            else None
        )
        while not stop_event.is_set():
            now = time.monotonic()
            deadlines = [fallback_deadline]
            if settle_deadline is not None:
                deadlines.append(settle_deadline)
            remaining = max(0.0, min(deadlines) - now)
            timeout = min(self.realtime_scan_interval, remaining)
            if timeout <= 0:
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                pass
            current = await asyncio.to_thread(self._snapshot_enabled_targets)
            if current != baseline:
                return
            if time.monotonic() >= min(deadlines):
                return

    def _snapshot_enabled_targets(
        self,
    ) -> dict[str, tuple[tuple[str, int, int], ...]]:
        """Build a metadata-only snapshot without following directory links."""

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

        snapshots: dict[str, tuple[tuple[str, int, int], ...]] = {}
        for raw_target in raw_targets:
            try:
                target = Path(raw_target).resolve(strict=True)
                if not target.is_dir() or target.is_symlink():
                    continue
                entries: list[tuple[str, int, int]] = []
                with os.scandir(target) as scanner:
                    for entry in scanner:
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            value = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        entries.append(
                            (
                                entry.name,
                                int(value.st_size),
                                int(value.st_mtime_ns),
                            )
                        )
                snapshots[str(target)] = tuple(sorted(entries))
            except (OSError, RuntimeError, ValueError):
                continue
        return snapshots

    def _resolve_runner(self) -> Any:
        if self._runner is not None:
            return self._runner

        source = Path(__file__).resolve().parents[1] / "main.py"
        if not source.is_file():
            raise FileNotFoundError("File Sorter runtime module is unavailable.")

        module_key = hashlib.sha256(
            str(source).encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:16]
        module_name = f"_gptbridge_file_sorter_runtime_{module_key}"
        existing = sys.modules.get(module_name)
        if isinstance(existing, ModuleType):
            self._runner = existing
            return existing

        spec = importlib.util.spec_from_file_location(module_name, source)
        if spec is None or spec.loader is None:
            raise ImportError("File Sorter runtime module could not be loaded.")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise

        for required_name in ("recover_transactions", "run_enabled_profiles_once"):
            if not callable(getattr(module, required_name, None)):
                sys.modules.pop(module_name, None)
                raise ImportError(
                    f"File Sorter runtime is missing {required_name}()."
                )

        self._runner = module
        return module

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
