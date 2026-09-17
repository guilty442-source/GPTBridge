"""Bounded on-disk sink for the supervised backend's stdout/stderr.

The Electron launcher spawns boot_core with ``stdio: 'ignore'``, so the
relay output boot_core forwards to its own stdout is discarded.  This module
persists the same relay stream to
``main-system/runtime/logs/backend-YYYYMMDD.log`` so backend failures stay
diagnosable after the process is gone.

Design constraints:

* Inline UTC timestamps on every line.
* Bounded rotation: one active file capped at 10 MiB and at most five files
  total (``backend-YYYYMMDD.log`` plus ``.1`` ... ``.4``); residue from
  previous days is pruned on open so the directory stays bounded.
* Fail-open: any filesystem error disables the sink with a single warning;
  the caller (``BootCore._relay``) must never crash because logging failed.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final

DEFAULT_MAX_BYTES: Final[int] = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT: Final[int] = 4
DEFAULT_MAX_FILES: Final[int] = DEFAULT_BACKUP_COUNT + 1
_LOG_NAME_TEMPLATE: Final[str] = "backend-{date}.log"
_LOG_GLOB: Final[str] = "backend-????????.log*"


class BackendLogSink:
    """Append-only rotating log writer that never raises at the caller."""

    def __init__(
        self,
        log_dir: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._handle: Any = None
        self._base_path: Path | None = None
        self._date = ""
        self._size = 0
        self._disabled = False
        self._disabled_reason = ""

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def write_line(self, line: str) -> bool:
        """Persist one relay line.  Returns False once the sink is disabled."""
        if self._disabled:
            return False
        try:
            return self._write_locked(str(line))
        except OSError as error:
            self._disable(error)
            return False
        except Exception as error:  # defensive fail-open
            self._disable(error)
            return False

    def close(self) -> None:
        with self._lock:
            self._close_handle()

    # -- internals ---------------------------------------------------

    def _write_locked(self, line: str) -> bool:
        with self._lock:
            now = self._now()
            self._ensure_open(now)
            stamp = now.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
            payload = f"[{stamp}] {line}\n".encode("utf-8", errors="replace")
            if self._size > 0 and self._size + len(payload) > self.max_bytes:
                self._rotate()
            handle = self._handle
            if handle is None:
                return False
            handle.write(payload)
            handle.flush()
            self._size += len(payload)
            return True

    def _ensure_open(self, now: datetime) -> None:
        date = now.strftime("%Y%m%d")
        if self._handle is not None and date == self._date:
            return
        self._close_handle()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._prune_previous_days()
        base = self.log_dir / _LOG_NAME_TEMPLATE.format(date=date)
        self._handle = open(base, "ab")
        self._base_path = base
        self._date = date
        try:
            self._size = base.stat().st_size
        except OSError:
            self._size = 0

    def _prune_previous_days(self) -> None:
        # The new base file is created after pruning, so keep one slot less
        # than the total file budget; within-day rotation stays bounded.
        keep = self.backup_count
        candidates: list[tuple[float, Path]] = []
        for path in self.log_dir.glob(_LOG_GLOB):
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, path in candidates[keep:]:
            try:
                path.unlink()
            except OSError:
                pass

    def _rotate(self) -> None:
        base = self._base_path
        self._close_handle()
        if base is None:
            return
        if self.backup_count <= 0:
            base.unlink(missing_ok=True)
        else:
            base.with_name(f"{base.name}.{self.backup_count}").unlink(
                missing_ok=True
            )
            for index in range(self.backup_count - 1, 0, -1):
                source = base.with_name(f"{base.name}.{index}")
                if source.exists():
                    os.replace(
                        source, base.with_name(f"{base.name}.{index + 1}")
                    )
            if base.exists():
                os.replace(base, base.with_name(f"{base.name}.1"))
        self._handle = open(base, "ab")
        self._size = 0

    def _close_handle(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _disable(self, error: BaseException) -> None:
        if self._disabled:
            return
        self._disabled = True
        self._disabled_reason = f"{type(error).__name__}: {error}"
        try:
            with self._lock:
                self._close_handle()
        except Exception:
            pass
        try:
            sys.stderr.write(
                "[boot-core] backend log sink disabled (fail-open): "
                f"{self._disabled_reason}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass


_SINK_LOCK = threading.Lock()
_SINK: BackendLogSink | None = None


def get_backend_log_sink(log_dir: str | os.PathLike[str]) -> BackendLogSink:
    """Return the process-wide sink for ``log_dir`` (one handle per process)."""
    global _SINK
    resolved = Path(log_dir)
    with _SINK_LOCK:
        if _SINK is None or _SINK.log_dir != resolved:
            _SINK = BackendLogSink(resolved)
        return _SINK


def reset_backend_log_sink() -> None:
    """Close and drop the process-wide sink (tests and shutdown paths)."""
    global _SINK
    with _SINK_LOCK:
        if _SINK is not None:
            _SINK.close()
        _SINK = None


__all__ = [
    "BackendLogSink",
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FILES",
    "get_backend_log_sink",
    "reset_backend_log_sink",
]
