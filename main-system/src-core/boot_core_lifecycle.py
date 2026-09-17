"""BootCore lifecycle mixin — spawn, relay, terminate, readiness.

Provides backend process spawning, output relay, process termination,
and backend readiness waiting for the BootCore supervisor.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from backend_log_sink import get_backend_log_sink


class BootCoreLifecycleMixin:
    """Child lifecycle methods for BootCore."""

    def _python_executable(self) -> str:
        exe = Path(sys.executable).resolve()
        if os.name == "nt":
            pythonw = exe.with_name("pythonw.exe")
            if pythonw.is_file():
                return os.fspath(pythonw)
        return os.fspath(exe)

    def _spawn_backend(
        self, args: list[str], startup_state: str = "", generation_id: str = "",
        backend_port: int | None = None,
    ) -> subprocess.Popen[bytes]:
        command = [
            self._python_executable(),
            "-u",
            "-B",
            os.fspath(self.backend_entry),
            "--serve",
            *args,
        ]
        creationflags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
            | int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
        )
        env = dict(os.environ)
        # Generate a fresh governance bootstrap token for each spawn so the
        # 30-second identity attestation expiry is always within window.
        try:
            # Bootstrap attestations are single-use. Never inherit a token
            # consumed by the previous backend process.
            env["GPTBRIDGE_GOVERNANCE_BOOTSTRAP"] = (
                self._generate_governance_bootstrap()
            )
        except Exception as error:
            self._last_exit = {
                "error": f"governance-bootstrap-failed: {type(error).__name__}: {error}"
            }
            self._status = "governance-bootstrap-failed"
            self._write_state()
            raise
        env["GPTBRIDGE_PROJECT_ROOT"] = str(self.workspace_root)
        env["GPTBRIDGE_WORKSPACE_ROOT"] = str(self.workspace_root)
        if startup_state:
            env["GPTBRIDGE_STARTUP_STATE"] = startup_state
        if generation_id:
            env["GPTBRIDGE_STARTUP_GENERATION"] = generation_id
        if backend_port is not None:
            env["GPTBRIDGE_IPC_PORT"] = str(backend_port)
            env["GPTBRIDGE_GATEWAY_PORT"] = str(self._health_probe_port)
        return subprocess.Popen(  # noqa: S603 - governed local spawn
            command,
            cwd=os.fspath(self.project_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    def _relay(self, stream: object) -> None:
        """Forward child output so the launcher sees readiness lines.

        Also captures the last N lines into ``_child_output`` so that
        ``_run_auto_repair`` can diagnose the actual crash cause instead
        of running a blind full-source scan, and persists every line to the
        rotating backend log under ``main-system/runtime/logs``.

        The disk sink is fail-open: a logging failure records one warning
        (stderr plus the in-memory diagnostic buffer) and disables
        persistence without ever interrupting the relay.
        """

        sink = None
        try:
            sink = get_backend_log_sink(
                self.workspace_root / "main-system" / "runtime" / "logs"
            )
        except Exception as error:
            self._warn_log_sink_failure(error)

        try:
            for raw in iter(stream.readline, b""):
                try:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    return
                line = ""
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
                    with self._child_output_lock:
                        self._child_output.append(line)
                        # Keep only the last 200 lines — enough for any
                        # realistic traceback without unbounded memory.
                        if len(self._child_output) > 200:
                            del self._child_output[:100]
                except Exception:
                    pass
                if sink is not None and not sink.disabled:
                    try:
                        if not sink.write_line(line):
                            # The sink already warned on stderr; surface the
                            # same warning in the diagnostic buffer only.
                            self._warn_log_sink_failure(
                                sink.disabled_reason, emit_stderr=False
                            )
                            sink = None
                    except Exception as error:
                        self._warn_log_sink_failure(error)
                        sink = None
        except (ValueError, OSError):
            return

    def _warn_log_sink_failure(
        self, error: object, *, emit_stderr: bool = True
    ) -> None:
        """Record a fail-open backend-log-sink warning without raising."""
        if isinstance(error, str) and error:
            message = error
        else:
            message = f"{type(error).__name__}: {error}"
        warning = f"[boot-core] backend log sink disabled (fail-open): {message}"
        if emit_stderr:
            try:
                sys.stderr.write(warning + "\n")
                sys.stderr.flush()
            except Exception:
                pass
        try:
            with self._child_output_lock:
                self._child_output.append(warning)
                if len(self._child_output) > 200:
                    del self._child_output[:100]
        except Exception:
            pass

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

    def _terminate_process(self, child: subprocess.Popen[bytes]) -> None:
        if child.poll() is not None:
            return
        try:
            child.terminate()
            child.wait(timeout=10)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass

    def _wait_backend_ready(
        self, child: subprocess.Popen[bytes], port: int, timeout: float
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if child.poll() is not None:
                return False
            if self._probe_health(port):
                return True
            self._stop.wait(self._startup_health_probe_interval)
        return False

    @staticmethod
    def _wait_port_available(port: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                    return True
                except OSError:
                    time.sleep(0.1)
        return False
