"""BootCore lifecycle mixin — spawn, relay, terminate, readiness.

Provides backend process spawning, output relay, process termination,
and backend readiness waiting for the BootCore supervisor.
"""

from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

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
        # No -B: persist bytecode caches for fast restarts (mtime-safe).
        command = [
            self._python_executable(),
            "-u",
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
        # P110/E173: propagate the complete-startup deadline epoch so the
        # backend executor measures against boot_core's cycle start, not
        # its own (pre-spawn gate time consumes the same 10 s budget).
        boot_epoch = getattr(self, "_boot_epoch_wall", 0.0)
        if boot_epoch:
            env["GPTBRIDGE_BOOT_EPOCH"] = str(boot_epoch)
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
        sink_warned_reason: str | None = None
        try:
            sink = get_backend_log_sink(
                self.workspace_root / "main-system" / "runtime" / "logs"
            )
        except Exception as error:
            self._warn_log_sink_failure(error)
        if sink is not None and sink.disabled:
            # Warn before draining: the diagnostic must precede relayed
            # payload lines in the in-memory buffer, never displace them.
            self._warn_log_sink_failure(sink.disabled_reason)
            sink_warned_reason = sink.disabled_reason

        # pythonw.exe / detached launchers have sys.stdout=None (or a dead
        # pipe).  The relay must keep draining the child's stdout anyway —
        # returning early leaves the pipe full and deadlocks the backend.
        #
        # All slow I/O (disk sink flush, own-stdout forward) runs on a
        # dedicated writer thread behind a bounded queue: a stalling
        # filesystem must never backpressure this read loop, because a
        # full child pipe freezes the backend's event loop at print().
        relay_out = getattr(sys.stdout, "buffer", None)
        work: queue.Queue = queue.Queue(maxsize=8192)
        dropped = 0

        def _writer() -> None:
            nonlocal relay_out
            pending: list[tuple[bytes, str]] = []
            while True:
                try:
                    item = work.get(timeout=0.25)
                except queue.Empty:
                    if pending:
                        relay_out = self._flush_relay_batch(
                            pending, sink, relay_out
                        )
                        pending = []
                    continue
                if item is None:
                    if pending:
                        self._flush_relay_batch(pending, sink, relay_out)
                    return
                pending.append(item)
                if len(pending) >= 128:
                    relay_out = self._flush_relay_batch(
                        pending, sink, relay_out
                    )
                    pending = []

        writer = threading.Thread(
            target=_writer, name="backend-relay-writer", daemon=True
        )
        writer.start()
        try:
            for raw in iter(stream.readline, b""):
                line = ""
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
                    with self._child_output_lock:
                        self._child_output.append(line)
                except Exception:
                    pass
                try:
                    work.put_nowait((raw, line))
                except queue.Full:
                    dropped += 1
                    if dropped == 1 or dropped % 512 == 0:
                        self._warn_log_sink_failure(
                            f"relay queue full — {dropped} backend log "
                            "line(s) dropped (sink backpressure)",
                            emit_stderr=False,
                        )
            for _ in range(10):
                if not writer.is_alive():
                    break
                try:
                    work.put(None, timeout=0.5)
                    break
                except queue.Full:
                    continue
            writer.join(timeout=5.0)
            if (
                sink is not None
                and sink.disabled
                and sink.disabled_reason != sink_warned_reason
            ):
                self._warn_log_sink_failure(
                    sink.disabled_reason, emit_stderr=False
                )
        except (ValueError, OSError):
            try:
                work.put_nowait(None)
            except queue.Full:
                pass
            return

    def _flush_relay_batch(self, pending, sink, relay_out):
        """Write one relay batch to the disk sink and own stdout.

        Runs on the relay writer thread only; failures here degrade to
        warnings and must never propagate into the relay read loop.
        Returns the (possibly cleared) ``relay_out`` for the next call.
        """
        raw = b"".join(item[0] for item in pending)
        if relay_out is not None:
            try:
                relay_out.write(raw)
                relay_out.flush()
            except (BrokenPipeError, OSError):
                relay_out = None
        if sink is not None and not sink.disabled:
            try:
                sink.write_lines([item[1] for item in pending])
            except Exception as error:
                self._warn_log_sink_failure(error)
        return relay_out

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
        except Exception:
            pass

    def _kill_process_tree(self, child: subprocess.Popen[bytes]) -> bool:
        """Terminate a spawned backend and all of its descendants.

        The spawned interpreter is a venv redirector (``pythonw.exe`` in the
        deployment venv) which launches the real backend interpreter as a
        child process.  ``Popen.terminate`` only signals the redirector —
        the real backend survives orphaned, keeps the IPC port bound, and
        turns into a zombie that answers nothing while the next
        generation's probes keep hitting it.  Kill the whole tree.
        """
        if os.name != "nt":
            return False
        try:
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0),
            )
        except Exception:
            return False
        try:
            child.wait(timeout=10)
        except Exception:
            pass
        return True

    def _terminate_child(self) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        if self._kill_process_tree(child):
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
        if self._kill_process_tree(child):
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
            # startup_dead latches permanently — the standby generation
            # can never become healthy; fail the wait immediately instead
            # of burning the whole readiness timeout.
            if getattr(self, "_backend_startup_dead", False):
                return False
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
