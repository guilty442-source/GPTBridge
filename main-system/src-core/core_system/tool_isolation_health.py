"""Tool isolation health monitoring and shutdown mixin.

Provides health checking, crash containment, graceful shutdown,
and the background monitoring loop for the ToolIsolationManager.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from typing import Any

import psutil

from core_system.tool_isolation_types import ToolIsolationEntry

_logger = logging.getLogger("gptbridge.tool_isolation")


class ToolIsolationHealthMixin:
    """Health monitoring, crash containment, and shutdown methods."""

    def check_tool_health(self, tool_id: str, *, light: bool = False) -> dict[str, Any]:
        """Check the health of a single tool. Returns a status dict.

        When light=True, only checks if the process is alive (no psutil CPU/memory).
        """
        with self._lock:
            entry = self._entries.get(tool_id)
            limits = (
                (entry.memory_limit_mb, entry.cpu_percent_limit, entry.restart_count)
                if entry is not None
                else None
            )
        if entry is None or limits is None:
            return {"tool_id": tool_id, "status": "not_registered"}

        memory_limit_mb, cpu_limit_percent, restart_count = limits
        try:
            proc = psutil.Process(entry.pid)
            if not proc.is_running():
                return {
                    "tool_id": tool_id,
                    "status": "crashed",
                    "pid": entry.pid,
                    "exit_code": entry.process.returncode,
                    "restart_count": restart_count,
                }
            if light:
                with self._lock:
                    if self._entries.get(tool_id) is entry:
                        entry.last_health_check = time.monotonic()
                return {
                    "tool_id": tool_id,
                    "status": "healthy",
                    "pid": entry.pid,
                    "restart_count": restart_count,
                }
            cpu = proc.cpu_percent(interval=None)
            mem_info = proc.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024)
            over_memory = memory_limit_mb > 0 and mem_mb > memory_limit_mb
            over_cpu = cpu_limit_percent > 0 and cpu > cpu_limit_percent * 1.5

            with self._lock:
                if self._entries.get(tool_id) is entry:
                    entry.last_health_check = time.monotonic()
                    entry.last_cpu_percent = cpu
                    entry.last_memory_mb = mem_mb

            return {
                "tool_id": tool_id,
                "status": "healthy" if not (over_memory or over_cpu) else "warning",
                "pid": entry.pid,
                "cpu_percent": round(cpu, 1),
                "memory_mb": round(mem_mb, 1),
                "memory_limit_mb": memory_limit_mb,
                "cpu_limit_percent": cpu_limit_percent,
                "over_memory": over_memory,
                "over_cpu": over_cpu,
                "restart_count": restart_count,
            }
        except psutil.NoSuchProcess:
            return {
                "tool_id": tool_id,
                "status": "crashed",
                "pid": entry.pid,
                "restart_count": restart_count,
            }
        except Exception as exc:
            return {
                "tool_id": tool_id,
                "status": "error",
                "error": str(exc),
            }

    def check_all_health(self) -> list[dict[str, Any]]:
        """Check health of all registered tools."""
        with self._lock:
            tool_ids = list(self._entries.keys())
        return [self.check_tool_health(tid) for tid in tool_ids]

    def register_crash_callback(self, callback) -> None:
        """Register a callback called when a tool crashes."""
        self._crash_callbacks.append(callback)

    def handle_crash(self, tool_id: str) -> dict[str, Any]:
        """Handle a tool crash: quarantine, notify, and optionally restart."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered"}

        policy = self.resolve_policy(tool_id)

        config = self._load_policy_config()
        crash_config = config.get("crash_containment", {})
        if crash_config.get("isolate_on_crash", True):
            self._quarantine_crash(tool_id, entry)

        for cb in self._crash_callbacks:
            try:
                cb(tool_id, entry)
            except Exception:
                pass

        if not policy.restart_on_crash:
            with self._lock:
                entry.crashed = True
            return {"tool_id": tool_id, "action": "quarantined", "restarted": False}

        with self._lock:
            entry.crashed = True
            if entry.restart_count >= policy.max_restart_attempts:
                _logger.warning(
                    "tool_crash_max_restarts tool_id=%s restarts=%d — giving up",
                    tool_id, entry.restart_count,
                )
                entry.quarantined = True
                return {
                    "tool_id": tool_id,
                    "action": "quarantined",
                    "restarted": False,
                    "reason": "max_restarts_exceeded",
                }

            backoff_idx = min(entry.restart_count, len(policy.restart_backoff_seconds) - 1)
            delay = policy.restart_backoff_seconds[backoff_idx]
            entry.restart_count += 1
            entry.last_restart_time = time.monotonic()
            attempt = entry.restart_count

        _logger.info(
            "tool_crash_restart tool_id=%s attempt=%d delay=%ds",
            tool_id, attempt, delay,
        )
        return {
            "tool_id": tool_id,
            "action": "restart_scheduled",
            "restarted": True,
            "delay_seconds": delay,
            "attempt": attempt,
        }

    def _quarantine_crash(self, tool_id: str, entry: ToolIsolationEntry) -> None:
        """Record crash info in the quarantine directory."""
        config = self._load_policy_config()
        crash_config = config.get("crash_containment", {})
        quarantine_dir = self.project_root / crash_config.get(
            "quarantine_dir",
            "main-system/runtime/state/tool-crash-quarantine",
        )
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "tool_id": tool_id,
                "pid": entry.pid,
                "restart_count": entry.restart_count,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "exit_code": entry.process.returncode,
            }
            path = quarantine_dir / f"{tool_id}-{int(time.time())}.json"
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            max_entries = int(crash_config.get("max_quarantine_entries", 20))
            entries = sorted(quarantine_dir.glob(f"{tool_id}-*.json"))
            for old_path in entries[:-max_entries]:
                old_path.unlink(missing_ok=True)
        except OSError:
            pass

    def shutdown_tool(self, tool_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Gracefully shut down a tool with signal escalation."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered"}

        policy = self.resolve_policy(tool_id)
        shutdown_timeout = timeout or policy.graceful_shutdown_timeout_seconds

        try:
            entry.process.terminate()
        except Exception:
            pass

        try:
            entry.process.wait(timeout=shutdown_timeout / 2)
            self.unregister_tool(tool_id)
            return {"tool_id": tool_id, "action": "terminated", "method": "SIGTERM"}
        except subprocess.TimeoutExpired:
            pass

        try:
            entry.process.terminate()
        except Exception:
            pass
        try:
            entry.process.wait(timeout=shutdown_timeout / 3)
            self.unregister_tool(tool_id)
            return {"tool_id": tool_id, "action": "terminated", "method": "SIGTERM_2"}
        except subprocess.TimeoutExpired:
            pass

        try:
            entry.process.kill()
            entry.process.wait(timeout=2.0)
        except Exception:
            pass
        self.unregister_tool(tool_id)
        return {"tool_id": tool_id, "action": "terminated", "method": "SIGKILL"}

    def shutdown_all(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Gracefully shut down all registered tools."""
        with self._lock:
            tool_ids = list(self._entries.keys())
        return [self.shutdown_tool(tid, timeout) for tid in tool_ids]

    def start_monitor(self, interval: float = 30.0, *, light: bool = True) -> None:
        """Start a background thread that periodically checks tool health.

        Args:
            interval: Check interval in seconds (default 30s).
            light: If True, use light checks (process alive only). If False, use
                   full psutil CPU/memory checks.
        """
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_light = light
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval,),
            name="tool-isolation-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        _logger.info("tool_isolation_monitor_started interval=%.1fs light=%s", interval, light)

    def stop_monitor(self) -> None:
        """Stop the health monitoring background thread."""
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None

    def _superseded_by_newer_generation(self) -> bool:
        """True when this backend generation has been replaced by a newer one."""
        generation = os.environ.get("GPTBRIDGE_STARTUP_GENERATION", "").strip()
        if not generation:
            return False
        try:
            state = json.loads(
                (
                    self.project_root
                    / "main-system"
                    / "runtime"
                    / "state"
                    / "boot-core.json"
                ).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False
        active = str(state.get("active_generation") or "").strip()
        return bool(active) and active != generation

    def _monitor_loop(self, interval: float) -> None:
        """Background health check loop — detects crashes and notifies."""
        light = getattr(self, "_monitor_light", True)
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    tool_ids = list(self._entries.keys())
                for tid in tool_ids:
                    if self._stop_event.is_set():
                        break
                    with self._lock:
                        entry = self._entries.get(tid)
                    if entry is not None and (entry.crashed or entry.quarantined):
                        continue
                    health = self.check_tool_health(tid, light=light)
                    if health.get("status") == "crashed":
                        if self._superseded_by_newer_generation():
                            with self._lock:
                                replaced = self._entries.get(tid)
                                if replaced is not None:
                                    replaced.crashed = True
                            continue
                        _logger.warning(
                            "tool_isolation_crash_detected tool_id=%s pid=%s",
                            tid, health.get("pid"),
                        )
                        self.handle_crash(tid)
                        for cb in self._crash_callbacks:
                            try:
                                cb(tid, self._entries.get(tid))
                            except Exception:
                                pass
            except Exception:
                pass
            self._stop_event.wait(timeout=interval)
