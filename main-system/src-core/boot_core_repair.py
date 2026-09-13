"""BootCore repair and watchdog mixin — connection watchdog, auto-repair, gateway.

Provides connection watchdog lifecycle, crash diagnosis signaling,
and gateway management for the BootCore supervisor.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any


class BootCoreRepairMixin:
    """Repair, watchdog, and gateway methods for BootCore."""

    def _start_connection_watchdog(self) -> None:
        """Start the connection watchdog thread."""
        try:
            self._ensure_runtime_paths()
            from tasks.connection_watchdog import ConnectionWatchdog
            from tasks.repair_learning import RepairLearningStore

            repair_data = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_data.mkdir(parents=True, exist_ok=True)
            learning_store = RepairLearningStore(repair_data)

            cwd = ConnectionWatchdog(
                self.project_root,
                health_port=self._health_probe_port,
            )
            cwd.set_learning_store(learning_store)
            cwd.set_repair_callback(self._on_connection_disconnected)
            self._connection_watchdog = cwd

            def backend_alive() -> bool:
                return self._child is not None and self._child.poll() is None

            self._watchdog = threading.Thread(
                target=cwd.run, args=(backend_alive,), daemon=True,
                name="connection-watchdog",
            )
            self._watchdog.start()
        except Exception:
            pass  # Watchdog is best-effort; never block boot_core.

    def _stop_connection_watchdog(self) -> None:
        """Stop the connection watchdog thread."""
        if self._connection_watchdog is not None:
            try:
                self._connection_watchdog.stop()
            except Exception:
                pass
        if self._watchdog is not None and self._watchdog.is_alive():
            self._watchdog.join(timeout=2.0)
        self._connection_watchdog = None
        self._watchdog = None

    def _on_connection_disconnected(self, failure_code: str, snapshot: Any) -> None:
        """Callback when the connection watchdog detects a persistent disconnection.

        A67 failure path: health-maintenance-test-sub-sovereign-classifies > decision-sovereign-decides
        > sub-sovereign-dispatch > governed-executor-repairs > boot-core-revalidates > ui-resynchronizes.
        A67 FORBID:duplicate-repair-owner — acquire the repair coordination
        lock before acting; if another owner already holds it, do not
        duplicate the repair.
        """
        try:
            self._ensure_runtime_paths()
            from tasks.repair_coordinator import RepairCoordinator

            # Cross-process coordination via the shared state file.
            coordinator = RepairCoordinator(self.project_root)
            if not coordinator.try_acquire(
                failure_code=failure_code,
                owner="boot-core-connection-watchdog",
            ):
                # Another owner is already repairing — do not duplicate.
                return

            from tasks.central_repair import CentralRepairService

            repair_root = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_root.mkdir(parents=True, exist_ok=True)
            service = CentralRepairService(self.project_root, repair_root)
            # Consult learned recipes with the consistent connection signature
            # (continuous learning: recorded outcomes are discoverable here).
            try:
                suggestion = service.suggest_connection_remedy(
                    failure_code,
                    getattr(snapshot, "overall_state", "unknown"),
                    "disconnected",
                )
                if suggestion.get("suggested"):
                    # A learned recipe exists — record that it was consulted.
                    service.record_connection_outcome(
                        failure_code,
                        getattr(snapshot, "overall_state", "unknown"),
                        "disconnected",
                        remedy=str(suggestion.get("remedy", "connection-watchdog")),
                        ok=False,
                        run_id=f"watchdog-{int(time.time())}",
                    )
            except Exception:
                pass
            # Record the connection failure for learning.
            service.record_connection_outcome(
                failure_code,
                getattr(snapshot, "overall_state", "unknown"),
                "disconnected",
                remedy="connection-watchdog",
                ok=False,
                run_id=f"watchdog-{int(time.time())}",
            )
            # Release the lock so the frontend or boot_core restart can proceed.
            coordinator.release(
                owner="boot-core-connection-watchdog",
                failure_code=failure_code,
            )
        except Exception:
            pass  # Learning is best-effort.

    def _start_gateway(self, allow_replacement: bool) -> None:
        try:
            self._gateway.start()
            return
        except OSError:
            if not allow_replacement:
                raise
        self._ensure_runtime_paths()
        from ipc.server_process import _get_port_owner, _is_gptbridge_process, _kill_process

        owner_pid, _description = _get_port_owner(self._health_probe_port)
        if (
            owner_pid is None
            or not _is_gptbridge_process(owner_pid, self.project_root)
            or not _kill_process(owner_pid)
        ):
            raise OSError(f"gateway port {self._health_probe_port} is occupied")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                self._gateway.start()
                return
            except OSError:
                time.sleep(0.1)
        raise OSError(f"gateway port {self._health_probe_port} did not release")

    def _signal_startup_failure(
        self, exit_code: int, uptime: float
    ) -> dict[str, object]:
        """Diagnose an early crash and submit a governed repair signal.

        This startup-owned path performs no repair mutation and makes no
        maintenance decision.
        """

        report: dict[str, object] = {
            "triggered": False,
            "reason": "",
            "ok": False,
        }
        # Development crashes are surfaced to the active developer and do not
        # create background repair requests.
        if os.environ.get("GPTBRIDGE_RENDERER_DEV_URL"):
            report["reason"] = "dev-mode; auto-repair disabled"
            return report

        # Only repair on crashes that happened early (likely startup failure
        # from corrupted source) and with a non-zero exit code.
        if exit_code == 0 or uptime >= self._crash_repair_uptime_threshold:
            report["reason"] = "crash-after-stable-uptime; repair not indicated"
            return report

        report["triggered"] = True
        report["reason"] = f"crash-exit-code-{exit_code}-uptime-{uptime:.1f}s"
        self._status = "auto-repairing"
        self._write_state(repair=report)

        try:
            self._ensure_runtime_paths()

            with self._child_output_lock:
                child_output = list(self._child_output)
            diagnosis = self._crash_diagnoser.diagnose(
                child_output, self.project_root
            )
            report["diagnosis"] = diagnosis

            from tasks.repair_coordinator import RepairCoordinator

            failure_code = str(diagnosis.get("failure_code") or "STARTUP_CRASH")
            signal_report = RepairCoordinator(
                self.project_root
            ).request_governed_repair(
                failure_code=failure_code,
                owner="startup-sub-sovereign",
                decision_proof={
                    "authority": "signal-only",
                    "exit_code": exit_code,
                    "uptime_seconds": round(uptime, 3),
                    "diagnosis": diagnosis,
                },
                signal_only=True,
            )
            report["ok"] = bool(signal_report.get("ok"))
            report["signal"] = signal_report
        except Exception as error:
            report["ok"] = False
            report["error"] = f"{type(error).__name__}: {error}"
        return report
