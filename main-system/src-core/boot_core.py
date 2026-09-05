"""boot_core — independent startup core (啟動核心).

Entry responsibility boundary (per architecture decision):

  * 啟動入口 (Electron main) — only wakes the screen; it spawns this
    startup core and does not manage the backend directly.
  * 啟動核心 (this process) — awakens the system core: it spawns the main
    backend (``main.py --serve``), which in turn awakens the sovereigns,
    and supervises the backend for its whole lifetime.
  * Single-fault isolation — a backend crash is restarted here with bounded
    backoff; if THIS process dies, the launcher's own recovery respawns it
    while the backend (if still alive) keeps serving.

The startup core is deliberately minimal and stdlib-only: it forwards the
environment untouched (the launcher-owned governance bootstrap material flows
straight through to the backend), relays the child's stdout/stderr so the
launcher can observe readiness lines, and persists a small status file for
maintenance oversight.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_RESTARTS = 10
BACKOFF_SCHEDULE_SECONDS = (2, 5, 10, 20, 30, 45, 60)
STATE_RELATIVE = ("main-system", "runtime", "state", "boot-core.json")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BootCore:
    """Spawn, relay, and supervise the main backend process."""

    def __init__(self) -> None:
        src_core = Path(__file__).resolve().parent
        self.project_root = src_core.parents[1]
        self.backend_entry = src_core / "main.py"
        self.state_path = self.project_root.joinpath(*STATE_RELATIVE)
        self._stop = threading.Event()
        self._child: subprocess.Popen[bytes] | None = None
        self._restarts = 0
        self._last_exit: dict[str, object] = {}
        self._status = "starting"

    # --------------------------------------------------------------
    # state
    # --------------------------------------------------------------

    def _write_state(self, **extra: object) -> None:
        payload = {
            "role": "boot-core",
            "status": self._status,
            "pid": os.getpid(),
            "backend_pid": self._child.pid if self._child is not None else None,
            "restarts": self._restarts,
            "max_restarts": MAX_RESTARTS,
            "last_exit": self._last_exit,
            "updated_at": _iso_now(),
            **extra,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            pass

    # --------------------------------------------------------------
    # child lifecycle
    # --------------------------------------------------------------

    def _spawn_backend(self, args: list[str]) -> subprocess.Popen[bytes]:
        command = [
            os.fspath(Path(sys.executable).resolve()),
            "-u",
            "-B",
            os.fspath(self.backend_entry),
            *args,
        ]
        creationflags = int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0
        )
        return subprocess.Popen(  # noqa: S603 - governed local spawn
            command,
            cwd=os.fspath(self.project_root),
            env=dict(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    def _relay(self, stream: object) -> None:
        """Forward child output so the launcher sees readiness lines."""

        try:
            for raw in iter(stream.readline, b""):
                try:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    return
        except (ValueError, OSError):
            return

    def _terminate_child(self) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        try:
            child.terminate()
            child.wait(timeout=10)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass

    def _install_signals(self) -> None:
        def _stop_handler(_signum: int, _frame: object) -> None:
            self._stop.set()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _stop_handler)
                except (OSError, ValueError):
                    pass

    # --------------------------------------------------------------
    # supervise loop
    # --------------------------------------------------------------

    def run(self, args: list[str]) -> int:
        self._install_signals()
        self._write_state()
        while not self._stop.is_set():
            try:
                self._child = self._spawn_backend(args)
            except OSError as error:
                self._status = "spawn-failed"
                self._last_exit = {"error": f"{type(error).__name__}: {error}"}
                self._write_state()
                return 2
            self._status = "backend-running"
            self._write_state()
            relay = threading.Thread(
                target=self._relay, args=(self._child.stdout,), daemon=True
            )
            relay.start()
            while not self._stop.is_set():
                code = self._child.poll()
                if code is not None:
                    break
                time.sleep(0.5)
            if self._stop.is_set():
                self._terminate_child()
                self._status = "stopped"
                self._write_state()
                return 0
            code = int(self._child.returncode or 0)
            self._last_exit = {"code": code, "at": _iso_now()}
            if code == 0:
                self._status = "backend-stopped-clean"
                self._write_state()
                return 0
            self._restarts += 1
            if self._restarts > MAX_RESTARTS:
                self._status = "restart-budget-exhausted"
                self._write_state()
                return 3
            delay = BACKOFF_SCHEDULE_SECONDS[
                min(self._restarts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
            ]
            self._status = "backend-restarting"
            self._write_state(next_retry_in_seconds=delay)
            if self._stop.wait(timeout=delay):
                self._status = "stopped"
                self._write_state()
                return 0
        self._status = "stopped"
        self._write_state()
        return 0


def main() -> int:
    return BootCore().run([*sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
