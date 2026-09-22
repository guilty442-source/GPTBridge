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
from pathlib import Path
from typing import Any

import psutil

from core_system.tool_isolation_types import ToolIsolationEntry

_logger = logging.getLogger("gptbridge.tool_isolation")

_QUARANTINE_DEFAULT_AGE_DAYS = 14.0
_SECONDS_PER_DAY = 24 * 60 * 60
_STDERR_TAIL_LINES = 40
_CRASH_FAILURE_CODE = "TOOL_RUNTIME_CRASH"


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
                if entry.expected_stop:
                    return {
                        "tool_id": tool_id,
                        "status": "stopped",
                        "pid": entry.pid,
                        "expected_stop": True,
                    }
                return {
                    "tool_id": tool_id,
                    "status": "crashed",
                    "pid": entry.pid,
                    "exit_code": (
                        entry.process.returncode
                        if entry.process.returncode is not None
                        else -1
                    ),
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
            if entry.expected_stop:
                return {
                    "tool_id": tool_id,
                    "status": "stopped",
                    "pid": entry.pid,
                    "expected_stop": True,
                }
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

    def mark_expected_stop(self, tool_id: str) -> None:
        """Mark a registered tool's upcoming stop as deliberate lifecycle.

        Force-close and pre-respawn sweeps terminate the process outside
        the isolation manager; without this marker the monitor reports the
        observed exit as a crash and pollutes the fault evidence.
        """
        with self._lock:
            entry = self._entries.get(tool_id)
            if entry is not None:
                entry.expected_stop = True

    def handle_crash(self, tool_id: str) -> dict[str, Any]:
        """Handle a tool crash: quarantine, notify, and signal repair."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered"}
        if entry.expected_stop:
            # Deliberate lifecycle stop (force-close/owner shutdown): the
            # observed exit is not a fault.  Keep the entry so the next
            # registration cleanly replaces it.
            return {"tool_id": tool_id, "action": "expected-stop"}

        policy = self.resolve_policy(tool_id)

        config = self._load_policy_config()
        crash_config = config.get("crash_containment", {})
        diagnosis = self._diagnose_crash(tool_id, entry)
        if crash_config.get("isolate_on_crash", True):
            self._quarantine_crash(tool_id, entry, diagnosis=diagnosis)

        for cb in self._crash_callbacks:
            try:
                cb(tool_id, entry)
            except Exception:
                pass

        self._signal_crash_repair(tool_id, entry, diagnosis)

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

    def _tool_stderr_tail(self, tool_id: str, entry: ToolIsolationEntry) -> list[str]:
        """Read the bounded stderr tail captured for the crashed process.

        Tool spawn redirects stderr to ``<log_root>/stderr.log`` so a crash
        leaves diagnosable evidence instead of an empty exit code.
        """
        log_root = entry.log_root or str(
            self.project_root / "main-system" / "runtime" / "logs" / "tools" / tool_id
        )
        path = Path(log_root) / "stderr.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-_STDERR_TAIL_LINES:]

    def _diagnose_crash(
        self, tool_id: str, entry: ToolIsolationEntry
    ) -> dict[str, Any]:
        """Build the crash diagnosis from captured stderr evidence.

        The stderr tail is parsed with the same ``CrashDiagnoser`` rules
        the boot core uses for backend crashes: an indentation-family
        traceback is ``targeted`` (source-mutation repair, user-gated);
        anything else is a ``runtime-recovery`` candidate (stability-tier:
        owned-database inspection plus governed artifact rebuild).  The
        decision itself stays with the decision-sovereign (A152).
        """
        stderr_tail = self._tool_stderr_tail(tool_id, entry)
        try:
            from tasks.crash_diagnosis import CrashDiagnoser

            diagnosis = dict(
                CrashDiagnoser().diagnose(stderr_tail, self.project_root)
            )
        except Exception:
            diagnosis = {
                "action": "fallback",
                "error_type": "",
                "file": "",
                "line": None,
            }
        diagnosis["tool_id"] = tool_id
        diagnosis["pid"] = entry.pid
        diagnosis["exit_code"] = (
            entry.process.returncode
            if entry.process.returncode is not None
            else -1
        )
        diagnosis["stderr_tail"] = stderr_tail
        diagnosis["action"] = (
            "targeted" if diagnosis.get("action") == "targeted" else "runtime-recovery"
        )
        return diagnosis

    def _signal_crash_repair(
        self, tool_id: str, entry: ToolIsolationEntry, diagnosis: dict[str, Any]
    ) -> None:
        """Write a governed repair signal for the crash (signal-only).

        The isolation monitor never repairs: it submits a signal to the
        information layer (``repair-requests.json``) so the maintenance
        health-classification chain and the decision-sovereign own the
        repair decision per A152/A154/E128.  Duplicate open requests for
        the same tool are suppressed so a crash loop cannot flood the
        confirmation surface.
        """
        try:
            from tasks.repair_coordinator import RepairCoordinator

            coordinator = RepairCoordinator(self.project_root)
            for request in coordinator._read_requests():
                if str(request.get("status") or "") not in (
                    "pending",
                    "awaiting-confirmation",
                    "executing",
                ):
                    continue
                proof = request.get("decision_proof") or {}
                request_diagnosis = proof.get("diagnosis") or {}
                if str(request_diagnosis.get("tool_id") or "") == tool_id:
                    return
            coordinator.request_governed_repair(
                failure_code=_CRASH_FAILURE_CODE,
                owner="tool-isolation-monitor",
                decision_proof={
                    "source": "tool-isolation-crash-monitor",
                    "component_id": tool_id,
                    "diagnosis": diagnosis,
                    "stderr_tail": diagnosis.get("stderr_tail") or [],
                    "evidence": {
                        "fault_code": _CRASH_FAILURE_CODE,
                        "tool_id": tool_id,
                        "component": tool_id,
                        "file": diagnosis.get("file") or "",
                    },
                    "exit_context": {
                        "exit_code": diagnosis.get("exit_code"),
                        "stderr_lines": len(diagnosis.get("stderr_tail") or []),
                    },
                },
                signal_only=True,
            )
        except Exception:
            pass  # Best-effort: signaling never blocks containment.

    def _quarantine_crash(
        self,
        tool_id: str,
        entry: ToolIsolationEntry,
        *,
        diagnosis: dict[str, Any] | None = None,
    ) -> None:
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
                "exit_code": (
                    entry.process.returncode
                    if entry.process.returncode is not None
                    else -1
                ),
            }
            if diagnosis is not None:
                record["diagnosis"] = {
                    "action": diagnosis.get("action"),
                    "error_type": diagnosis.get("error_type"),
                    "file": diagnosis.get("file"),
                    "line": diagnosis.get("line"),
                    "stderr_tail": (diagnosis.get("stderr_tail") or [])[-20:],
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

    def purge_stale_quarantine(
        self,
        *,
        max_age_days: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Retention sweep for aged crash-quarantine evidence.

        Crash records are bounded per tool by ``max_quarantine_entries``;
        this sweep additionally drops records older than the configured
        ``max_quarantine_age_days`` so retired tools (whose records can
        never rotate out through new crashes) do not accumulate forever.
        Fail-open: a single unreadable or locked record is reported, never
        raised, and the newest records always survive.
        """

        config = self._load_policy_config()
        crash_config = config.get("crash_containment", {})
        retention_days = max_age_days
        if retention_days is None:
            raw_retention = crash_config.get(
                "max_quarantine_age_days", _QUARANTINE_DEFAULT_AGE_DAYS
            )
            try:
                retention_days = float(raw_retention)
            except (TypeError, ValueError):
                retention_days = _QUARANTINE_DEFAULT_AGE_DAYS
        retention_days = max(0.0, float(retention_days))
        quarantine_dir = self.project_root / crash_config.get(
            "quarantine_dir",
            "main-system/runtime/state/tool-crash-quarantine",
        )
        current = time.time() if now is None else float(now)
        cutoff = current - retention_days * _SECONDS_PER_DAY
        removed: list[str] = []
        failed: list[dict[str, str]] = []
        if quarantine_dir.is_dir():
            for record_path in sorted(quarantine_dir.glob("*.json")):
                if record_path.is_symlink() or not record_path.is_file():
                    continue
                try:
                    if record_path.stat().st_mtime >= cutoff:
                        continue
                    record_path.unlink()
                    removed.append(record_path.name)
                except OSError as error:
                    failed.append(
                        {
                            "path": record_path.name,
                            "reason": f"{type(error).__name__}: {error}",
                        }
                    )
        return {
            "ok": not failed,
            "operation": "tool-crash-quarantine-retention",
            "authority": "health-maintenance-test-sub-sovereign",
            "quarantine_dir": str(quarantine_dir),
            "retention_days": retention_days,
            "removed_count": len(removed),
            "removed": removed[:50],
            "failed": failed[:20],
        }

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
                    if entry is not None and (
                        entry.crashed or entry.quarantined or entry.expected_stop
                    ):
                        continue
                    health = self.check_tool_health(tid, light=light)
                    if health.get("status") == "crashed":
                        # TOCTOU guard: a governed stop may have marked
                        # expected_stop between the flag check above and the
                        # health verdict — re-read the entry before declaring
                        # a crash.
                        with self._lock:
                            entry = self._entries.get(tid)
                        if entry is not None and (
                            entry.crashed or entry.quarantined or entry.expected_stop
                        ):
                            continue
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
                        # handle_crash already invokes crash callbacks;
                        # do not duplicate the notification here.
                        self.handle_crash(tid)
            except Exception as exc:
                _logger.error("tool_isolation_monitor_error: %s", exc, exc_info=True)
            self._stop_event.wait(timeout=interval)
