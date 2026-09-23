"""Single-instance guard for the main-system backend.

Governor directive 2026-09-17: 自動退出舊進程，系統只能有一個進程 — starting a
new system generation must make older backend processes exit automatically,
and two live backend generations for the same workspace are forbidden.

Design:

- one lock document per (workspace, role) under ``runtime/state/singletons/``;
- ``acquire()`` terminates a live lock owner of the same role and sweeps any
  other live process that matches the role fingerprint (unlocked stale
  generations), excluding this process and its ancestors/descendants;
- a process that cannot be terminated raises ``SingletonBusyError``: the new
  generation fails closed instead of running next to the old one;
- ``release()`` removes the lock only when it belongs to this process.

Independent tools are separate single-instance entities (A266) and are not
touched by this guard.
"""

from __future__ import annotations

import atexit
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class SingletonBusyError(RuntimeError):
    """A previous generation is alive and could not be terminated."""


class ProcessTable(Protocol):
    """Minimal process inspection/termination surface (injectable for tests)."""

    def commandlines(self) -> dict[int, str]: ...

    def excluded_pids(self) -> set[int]: ...

    def kill(self, pid: int) -> bool: ...

    def sleep(self, seconds: float) -> None: ...


class _PsutilProcessTable:
    """Default table: native process queries (P24), taskkill for termination.

    Kept under the original name — the public contract is ``ProcessTable``
    and the class is still injectable for tests.
    """

    def __init__(self) -> None:
        from shared_layer.performance import process_metrics

        self._metrics = process_metrics

    def commandlines(self) -> dict[int, str]:
        result: dict[int, str] = {}
        for pid in self._metrics.process_list():
            cmdline = self._metrics.process_cmdline(pid)
            if cmdline:
                result[pid] = cmdline
        return result

    def excluded_pids(self) -> set[int]:
        excluded = {os.getpid()}
        excluded.update(self._metrics.process_children(os.getpid()))
        # P24: ancestor chain via native ppid walk (bounded depth).
        excluded.update(self._metrics.process_ancestors(os.getpid()))
        return excluded

    def kill(self, pid: int) -> bool:
        if os.name != "nt":
            return False
        import subprocess

        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0),
            )
            return completed.returncode == 0
        except Exception:
            return False

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


class ServerSingleton:
    """One live backend generation per workspace and role."""

    def __init__(
        self,
        project_root: Path | str,
        *,
        role: str = "main-backend",
        entry_markers: tuple[str, ...] = ("src-core\\main.py", "src-core/main.py"),
        table: ProcessTable | None = None,
        state_dir: Path | str | None = None,
        kill_timeout_seconds: float = 5.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.role = str(role).strip() or "main-backend"
        self.entry_markers = tuple(marker.casefold() for marker in entry_markers)
        self._table = table if table is not None else _PsutilProcessTable()
        self._lock_path = (
            Path(state_dir) if state_dir is not None else self.project_root / "runtime" / "state" / "singletons"
        ) / f"{self.role}.json"
        self._kill_timeout_seconds = max(0.0, float(kill_timeout_seconds))
        self._acquired = False

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def _matches_role(self, commandline: str) -> bool:
        normalized = str(commandline or "").casefold()
        if not normalized:
            return False
        if str(self.project_root).casefold() not in normalized.replace("/", "\\"):
            return False
        return any(marker in normalized.replace("/", "\\") for marker in self.entry_markers)

    def _read_lock(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _wait_until_gone(self, pid: int) -> bool:
        deadline = time.monotonic() + self._kill_timeout_seconds
        while time.monotonic() < deadline:
            if pid not in self._table.commandlines():
                return True
            self._table.sleep(0.1)
        return pid not in self._table.commandlines()

    def _terminate(self, pid: int, excluded: set[int]) -> bool:
        """Terminate one old generation; "already gone" counts as success."""
        if pid in excluded:
            return True
        if not self._table.kill(pid):
            # A failed kill often means the generation exited between the
            # liveness check and the termination call (the handover standby
            # races the old backend's own exit, and taskkill reports failure
            # for a pid that no longer exists).  Treat "already gone" as
            # success instead of failing the certified handover closed on a
            # process that no longer exists.
            return pid not in self._table.commandlines()
        return self._wait_until_gone(pid)

    def acquire(self) -> None:
        """Exit older same-role generations, then claim the lock.

        Raises :class:`SingletonBusyError` when an old generation cannot be
        terminated; the caller must not start a second backend generation.
        """
        excluded = set(self._table.excluded_pids())
        commandlines = self._table.commandlines()
        lock = self._read_lock()
        lock_pid = 0
        if lock is not None:
            try:
                lock_pid = int(lock.get("pid") or 0)
            except (TypeError, ValueError):
                lock_pid = 0
        if lock_pid and lock_pid in commandlines and lock_pid not in excluded:
            if not self._terminate(lock_pid, excluded):
                raise SingletonBusyError(
                    f"previous backend generation {lock_pid} did not exit"
                )
            commandlines = self._table.commandlines()
        for pid, commandline in sorted(commandlines.items()):
            if pid in excluded or not self._matches_role(commandline):
                continue
            if not self._terminate(pid, excluded):
                raise SingletonBusyError(
                    f"stale backend generation {pid} did not exit"
                )
        self._write_lock()
        self._acquired = True
        atexit.register(self.release)

    def _write_lock(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._lock_path.with_name(self._lock_path.name + ".tmp")
        payload = {
            "pid": os.getpid(),
            "role": self.role,
            "workspace": str(self.project_root),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._lock_path)

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        lock = self._read_lock()
        try:
            lock_pid = int((lock or {}).get("pid") or 0)
        except (TypeError, ValueError):
            lock_pid = 0
        if lock_pid != os.getpid():
            return
        try:
            self._lock_path.unlink()
        except OSError:
            return


__all__ = ["ProcessTable", "ServerSingleton", "SingletonBusyError"]
