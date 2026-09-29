"""Git automation supervision as a main-system task.

The governed git work itself — self-commit sweep, workspace sync, merge
queue, hooks — lives in the resident C# host
``GPTBridge.GitAutomation.exe --watch`` (C66: single scheduler, single
coordinator; Python carries zero residency for the git domain).  This
task owns only the host's *process lifecycle*:

- ``start()`` registers the ``git-automation`` flow with AutomationCore
  (or the shared scheduler) and spawns the resident host.  A denied
  registration never falls back to a private loop and never spawns.
- Each registered tick is a liveness probe: the host is respawned only
  within a bounded restart budget (never unbounded restart loops, and
  never respawned while the flow is disabled).
- ``stop()`` writes the governed stop sentinel
  (``runtime/state/git-automation.stop``) the host polls between cycles,
  then escalates to terminate/taskkill only if it does not exit.

The host owns ``runtime/state/git-automation.json``; this service keeps
its own supervisor state in ``runtime/state/git-automation-supervisor.json``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.git_automation")

_DEFAULT_SWEEP_INTERVAL = 60.0
_DEFAULT_SYNC_INTERVAL = 300.0
_DEFAULT_DEBOUNCE_SECONDS = 60.0

# Bounded respawn (A170/A178): at most 3 restarts inside a rolling 600 s
# window; a host that survived >= this uptime does not count as a crash.
_MAX_RESTARTS = 3
_RESTART_WINDOW_S = 600.0
_HEALTHY_UPTIME_S = 300.0
_STOP_GRACE_S = 15.0
_HOST_LOG_CAP_BYTES = 256 * 1024
_HOST_EXE_ENV = "GPTBRIDGE_GIT_AUTOMATION_EXE"
_HOST_EXE_CANDIDATES = (
    "shared-layer/csharp/GPTBridge.GitAutomation/"
    "publish/GPTBridge.GitAutomation.exe",
    "shared-layer/csharp/GPTBridge.GitAutomation/"
    "bin/Release/net10.0/GPTBridge.GitAutomation.exe",
)


class GitAutomationService:
    """Lifecycle supervisor for the resident C# git-automation host."""

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
        self._child: subprocess.Popen[bytes] | None = None
        self._spawned_at = 0.0
        self._restarts: deque[float] = deque()
        self._degraded_reason = ""
        self._scheduler = scheduler
        self._automation_core = automation_core

    # -- governed runtime paths (project_root-relative — the C# host
    #    resolves every one of these under its --root) -----------------

    def _runtime_state_dir(self) -> Path:
        return (self.project_root / "main-system"
                / "runtime" / "state")

    def _state_file(self) -> Path:
        return self._runtime_state_dir() / "git-automation.json"

    def _supervisor_state_file(self) -> Path:
        return (self._runtime_state_dir()
                / "git-automation-supervisor.json")

    def _stop_sentinel(self) -> Path:
        return self._runtime_state_dir() / "git-automation.stop"

    def _host_log(self) -> Path:
        return (self.project_root / "main-system"
                / "runtime" / "logs" / "git-automation-host.log")

    # -- host executable ------------------------------------------------

    def _host_exe(self) -> Path | None:
        """Resolve the governed host binary (env override first)."""
        override = os.environ.get(_HOST_EXE_ENV)
        if override and override.strip():
            candidate = Path(override.strip())
            return candidate if candidate.is_file() else None
        for relative in _HOST_EXE_CANDIDATES:
            candidate = self.project_root / relative
            if candidate.is_file():
                return candidate
        return None

    def _host_command(self, exe: Path) -> list[str]:
        command = [
            str(exe),
            "--watch",
            "--root", str(self.project_root),
            "--interval", str(int(self.sweep_interval)),
            "--debounce", str(int(self.debounce_seconds)),
            "--sync-interval", str(int(self.sync_interval)),
        ]
        # Push is governed by the manifest; the constructor flag is only
        # a fallback when no automation core is the registration point.
        if self.push and self._automation_core is None:
            command.append("--push")
        return command

    def _spawn_host(self) -> bool:
        exe = self._host_exe()
        if exe is None:
            self._degraded_reason = "host-exe-missing"
            _logger.error(
                "git automation host binary not found under %s",
                self.project_root,
            )
            return False
        host_log = self._host_log()
        try:
            host_log.parent.mkdir(parents=True, exist_ok=True)
            if (host_log.is_file()
                    and host_log.stat().st_size > _HOST_LOG_CAP_BYTES):
                keep = host_log.read_bytes()[-_HOST_LOG_CAP_BYTES // 2:]
                host_log.write_bytes(keep)
            log = host_log.open("ab")
        except OSError:
            log = subprocess.DEVNULL  # type: ignore[assignment]
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._child = subprocess.Popen(  # noqa: S603 - governed host
                self._host_command(exe),
                cwd=str(self.project_root),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except OSError as error:
            self._degraded_reason = f"spawn-failed:{error}"
            _logger.error("git automation host spawn failed: %s", error)
            return False
        finally:
            # The child owns a dup of the handle; never hold the parent's
            # copy open for the host's lifetime.
            if log is not subprocess.DEVNULL:
                try:
                    log.close()
                except OSError:
                    pass
        self._spawned_at = time.monotonic()
        self._degraded_reason = ""
        _logger.info("git automation host started (pid=%s)",
                     self._child.pid)
        self._write_state()
        return True

    def _flow_enabled(self) -> bool:
        if self._automation_core is not None:
            try:
                return bool(
                    self._automation_core.is_enabled("git-automation"))
            except Exception:
                return False
        # Fallback path (no AutomationCore): read the governed manifest +
        # runtime override directly — same rule the C# host self-enforces,
        # so a disabled flow is never respawned even without the core.
        try:
            manifest = json.loads(
                (self.project_root / "main-system" / "config"
                 / "automation-flows.json").read_text(encoding="utf-8"))
            entry = manifest.get("flows", {}).get("git-automation") or {}
            if entry.get("enabled") is False:
                return False
            state = json.loads(
                (self.project_root / "main-system" / "runtime" / "state"
                 / "automation-flows-state.json").read_text(
                     encoding="utf-8"))
            override = (
                state.get("overrides", {}).get("git-automation") or {})
            if override.get("enabled") is False:
                return False
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        return True

    async def _stop_host(self) -> None:
        """Graceful governed stop: sentinel first, escalate bounded."""
        child = self._child
        self._child = None
        if child is None or child.poll() is not None:
            return
        try:
            sentinel = self._stop_sentinel()
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        try:
            await asyncio.to_thread(child.wait, _STOP_GRACE_S)
        except Exception:
            pass
        if child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass
            try:
                await asyncio.to_thread(child.wait, 5.0)
            except Exception:
                pass
        if child.poll() is None:
            try:
                await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(child.pid), "/T", "/F",
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, asyncio.CancelledError):
                pass

    # -- supervision tick ------------------------------------------------

    async def _supervise_tick(self) -> None:
        """Liveness probe — the only thing the registered tick does.

        All git work is inside the host; the tick never runs git work
        itself (no second scheduler).  Respawn is bounded; a denied or
        disabled flow stops the host instead of restarting it.
        """
        if self._stop_event.is_set():
            return
        if not self._flow_enabled():
            _logger.info(
                "git automation flow disabled — stopping host")
            await self._stop_host()
            return
        child = self._child
        if child is not None and child.poll() is None:
            self._write_state()
            return
        if child is not None:
            # Host exited.  A healthy-uptime exit resets the crash
            # budget; otherwise this start consumes a restart slot.
            uptime = time.monotonic() - self._spawned_at
            if uptime >= _HEALTHY_UPTIME_S:
                self._restarts.clear()
            _logger.warning(
                "git automation host exited (code=%s, uptime=%.0fs)",
                child.returncode, uptime)
            self._child = None
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > _RESTART_WINDOW_S:
            self._restarts.popleft()
        if len(self._restarts) >= _MAX_RESTARTS:
            self._degraded_reason = "restart-budget-exhausted"
            _logger.error(
                "git automation host restart budget exhausted "
                "(%d in %.0fs) — degraded, no respawn",
                len(self._restarts), _RESTART_WINDOW_S)
            self._write_state()
            return
        self._restarts.append(now)
        await asyncio.to_thread(self._spawn_host)
        self._write_state()

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        if not (self.project_root / ".git").exists():
            return {"status": "skipped", "reason": "not-a-git-worktree"}
        self._stop_event.clear()
        # Spawn BEFORE registering: a run_immediately registration can
        # invoke the tick on the next loop pass, and the tick owns
        # respawn decisions — an unspawned start would be double-counted.
        spawned = await asyncio.to_thread(self._spawn_host)
        if self._automation_core is not None:
            if self._automation_core.register_flow(
                "git-automation",
                self._supervise_tick,
                interval_s=self.sweep_interval,
                run_immediately=True,
                # The tick is a liveness probe only (poll + optional
                # spawn) — seconds, not the old sweep's minutes.
                timeout_s=60.0,
            ):
                _logger.info("git automation host supervised via "
                             "automation core (probe=%.0fs)",
                             self.sweep_interval)
                if spawned:
                    return {"status": "started", "loop": "automation-core"}
                return {"status": "degraded",
                        "reason": self._degraded_reason,
                        "loop": "automation-core"}
            # Registration denied → the spawned host must not survive
            # as a private bypass of the kill switch.
            await self._stop_host()
            _logger.info("git automation disabled by automation core")
            return {"status": "disabled", "loop": "automation-core"}
        if self._scheduler is not None:
            self._scheduler.register(
                "git-automation", self.sweep_interval,
                self._supervise_tick, run_immediately=True, pausable=True,
            )
            _logger.info("git automation host supervised on periodic "
                         "scheduler (probe=%.0fs)", self.sweep_interval)
            if spawned:
                return {"status": "started", "loop": "periodic-scheduler"}
            return {"status": "degraded",
                    "reason": self._degraded_reason,
                    "loop": "periodic-scheduler"}
        try:
            self._task = asyncio.create_task(
                self._loop(), name="git-automation"
            )
        except RuntimeError:
            self._task = None
            await self._stop_host()
            return {"status": "no_event_loop"}
        _logger.info("git automation host supervised "
                     "(probe=%.0fs)", self.sweep_interval)
        if spawned:
            return {"status": "started"}
        return {"status": "degraded", "reason": self._degraded_reason}

    async def _loop(self) -> None:
        """Private supervision loop — only when no scheduler exists."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._supervise_tick(), timeout=60.0)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _logger.warning("git automation supervise tick timeout")
            except Exception as error:  # never kill the loop
                _logger.warning(
                    "git automation supervise error: %s", error)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.sweep_interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            break

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
        await self._stop_host()
        self._write_state()

    # -- operations -----------------------------------------------------

    async def run_once_cycle(self) -> dict[str, Any]:
        """One-shot host cycle (``--once``) for manual verification.

        Bounded: the host is a subprocess with a hard timeout; a lock
        busy or disabled flow surfaces as a non-zero exit instead of
        hanging the caller.
        """
        exe = self._host_exe()
        if exe is None:
            return {"status": "degraded", "reason": "host-exe-missing"}
        command = [
            str(exe), "--once", "--root", str(self.project_root),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=900.0)
        except asyncio.TimeoutError:
            if process is not None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
            return {"status": "timeout"}
        except OSError as error:
            return {"status": "error", "reason": f"{error}"}
        text = output.decode("utf-8", errors="replace")
        return {
            "status": "ok" if process.returncode == 0 else "error",
            "exit_code": process.returncode,
            "output": text[-4000:],
        }

    # -- observability --------------------------------------------------

    def _host_state(self) -> dict[str, Any]:
        """The resident host's own published state (it owns the file)."""
        try:
            payload = json.loads(
                self._state_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def status(self) -> dict[str, Any]:
        child = self._child
        alive = child is not None and child.poll() is None
        return {
            "running": alive,
            "host": "GPTBridge.GitAutomation.exe --watch",
            "host_pid": child.pid if alive else None,
            "host_exit_code": (
                child.returncode if child is not None and not alive
                else None),
            "project_root": str(self.project_root),
            "sweep_interval": self.sweep_interval,
            "sync_interval": self.sync_interval,
            "debounce_seconds": self.debounce_seconds,
            "restarts": len(self._restarts),
            "degraded": self._degraded_reason or None,
            "host_state": self._host_state(),
        }

    def _write_state(self) -> None:
        payload = {"updated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        payload.update(self.status())
        state_file = self._supervisor_state_file()
        temporary = state_file.with_name(
            state_file.name + f".{os.getpid()}.tmp"
        )
        try:
            state_file.parent.mkdir(
                parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, state_file)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = ["GitAutomationService"]
