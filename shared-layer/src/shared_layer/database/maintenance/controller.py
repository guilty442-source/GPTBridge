"""Maintenance Controller.

Orchestrates the full maintenance lifecycle:
Observe → Evaluate → Policy → Budget → Lease → Execute → Verify → Record → Cooldown

Features:
- Safe stop/restart
- No blocking of normal runtime
- Graceful shutdown
- Auto-pause in recovery mode
- Generation change revalidation
- No single point of failure for core DB
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID

from .models import (
    MaintenanceJob,
    MaintenanceJobStatus,
    MaintenanceRiskClass,
    MaintenanceReasonCode,
)
from .registry import MaintenanceAction, get_registry
from .scheduler import MaintenanceScheduler, SchedulerConfig
from .verifier import Verifier, VerificationResult, verify_action
from .budgets import MaintenanceBudget
from .leases import get_lease_manager, release_lease
from .evaluator import evaluate_candidates


@dataclass(frozen=True)
class ControllerConfig:
    """Configuration for the maintenance controller."""

    # Scheduling
    scheduler_config: SchedulerConfig = field(default_factory=SchedulerConfig)

    # Execution
    max_concurrent_executions: int = 2
    execution_timeout_buffer_seconds: float = 30.0

    # Shutdown
    shutdown_timeout_seconds: float = 60.0
    max_shutdown_wait_seconds: float = 300.0

    # Recovery
    pause_on_recovery: bool = True
    revalidate_on_generation_change: bool = True

    # Persistence callbacks (set by integration)
    persist_job: Callable[[MaintenanceJob], None] | None = None
    load_pending_jobs: Callable[[], list[MaintenanceJob]] | None = None
    get_current_generation: Callable[[], int] | None = None
    get_system_state: Callable[[], dict[str, Any]] | None = None
    get_telemetry_signals: Callable[[], dict[str, Any]] | None = None


class MaintenanceController:
    """Main maintenance controller.

    Coordinates the full maintenance lifecycle without blocking normal operations.
    """

    def __init__(self, config: ControllerConfig | None = None) -> None:
        self.config = config or ControllerConfig()
        self._scheduler = MaintenanceScheduler(config=self.config.scheduler_config)
        self._verifier = Verifier()
        self._budget = MaintenanceBudget()
        self._lease_manager = get_lease_manager()

        self._running = False
        self._execution_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._pause_event = threading.Event()
        self._current_generation = 0

        # Execution tracking
        self._executing: dict[UUID, tuple[MaintenanceJob, MaintenanceAction, threading.Thread]] = {}

        # Callbacks (set by integration layer)
        self._persist_job = self.config.persist_job
        self._load_pending_jobs = self.config.load_pending_jobs
        self._get_generation = self.config.get_current_generation
        self._get_system_state = self.config.get_system_state
        self._get_signals = self.config.get_telemetry_signals

        # Engine executors (set by integration)
        self._executors: dict[str, Callable] = {}

    def set_executor(self, engine: str, executor: Callable) -> None:
        """Register an engine-specific executor."""
        self._executors[engine] = executor

    def set_native_shadow(self, shadow: Any) -> None:
        """Attach the §10.65 act-1 native shadow to the scheduler.
        ``None`` leaves the controller Python-only."""
        self._scheduler.set_native_shadow(shadow)

    def set_callbacks(
        self,
        persist_job: Callable[[MaintenanceJob], None] | None = None,
        load_pending_jobs: Callable[[], list[MaintenanceJob]] | None = None,
        get_current_generation: Callable[[], int] | None = None,
        get_system_state: Callable[[], dict[str, Any]] | None = None,
        get_telemetry_signals: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Set persistence and state callbacks."""
        if persist_job:
            self._persist_job = persist_job
        if load_pending_jobs:
            self._load_pending_jobs = load_pending_jobs
        if get_current_generation:
            self._get_generation = get_current_generation
        if get_system_state:
            self._get_system_state = get_system_state
        if get_telemetry_signals:
            self._get_signals = get_telemetry_signals

    def start(self, *, spawn_loop: bool = True) -> None:
        """Start the maintenance controller.

        ``spawn_loop=False`` starts without the private execution thread —
        the caller then drives cadence itself via :meth:`run_once`
        (§1.1 自動化集中：the automation core owns the schedule)."""
        with self._lock:
            if self._running:
                return

            # Recover pending jobs from persistence
            self._recover_pending_jobs()

            # Update generation
            if self._get_generation:
                self._current_generation = self._get_generation()
                self._scheduler.set_generation(self._current_generation)

            self._running = True
            self._shutdown_event.clear()
            self._pause_event.clear()

            # Start execution thread (skipped when externally driven)
            if spawn_loop:
                self._execution_thread = threading.Thread(
                    target=self._execution_loop,
                    name="maintenance-controller",
                    daemon=True,
                )
                self._execution_thread.start()

    def stop(self, graceful: bool = True) -> None:
        """Stop the maintenance controller."""
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._scheduler.set_draining(True)

            if graceful:
                # Wait for running jobs to complete (with timeout)
                self._wait_for_completion()

            self._shutdown_event.set()

            if self._execution_thread and self._execution_thread.is_alive():
                self._execution_thread.join(timeout=self.config.shutdown_timeout_seconds)

    def pause(self) -> None:
        """Pause maintenance scheduling (recovery mode)."""
        self._pause_event.set()
        self._scheduler.set_draining(True)

    def resume(self) -> None:
        """Resume maintenance scheduling."""
        self._pause_event.clear()
        self._scheduler.set_draining(False)

    def is_running(self) -> bool:
        return self._running

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def _recover_pending_jobs(self) -> None:
        """Recover pending jobs from persistence at startup."""
        if not self._load_pending_jobs:
            return

        try:
            pending = self._load_pending_jobs()
            for job in pending:
                if job.status in (MaintenanceJobStatus.RUNNING, MaintenanceJobStatus.VERIFYING):
                    # Job was interrupted - re-evaluate
                    if job.status == MaintenanceJobStatus.RUNNING:
                        # Check if lease is still valid
                        lease = self._lease_manager.get(
                            self._get_lease_scope(job)
                        )
                        if lease and not lease.is_expired():
                            # Lease still valid, job might still be running
                            # Mark as DEFERRED for re-evaluation
                            job = job.with_status(MaintenanceJobStatus.DEFERRED)
                        else:
                            # Lease expired, safe to retry
                            job = job.with_status(MaintenanceJobStatus.QUEUED)
                    elif job.status == MaintenanceJobStatus.VERIFYING:
                        # Verification was interrupted, re-verify
                        job = job.with_status(MaintenanceJobStatus.QUEUED)

                    # Requeue
                    self._scheduler.requeue_job(job.job_id, job.attempt_count)
        except Exception:
            # Log but don't fail startup
            pass

    def _get_lease_scope(self, job: MaintenanceJob) -> str:
        """Get lease scope for a job."""
        action = get_registry().get(job.action_id, job.action_version)
        if action:
            return action.lease_scope.format(
                database_id=job.database_id,
                module_id=job.module_id,
                backup_id=job.database_id,
                projection_name=job.database_id,
            )
        return f"{job.engine}:{job.action_id}:{job.database_id}"

    def run_once(self) -> None:
        """One scheduling/execution iteration, externally driven.

        §1.1 自動化集中：when the automation core owns the cadence
        (``start(spawn_loop=False)``) this body is invoked per scheduler
        tick instead of free-running on the private thread."""
        if self._pause_event.is_set():
            return
        # Check generation change
        if self._get_generation and self.config.revalidate_on_generation_change:
            new_gen = self._get_generation()
            if new_gen != self._current_generation:
                self._current_generation = new_gen
                self._scheduler.set_generation(new_gen)
                # Revalidate running jobs
                self._revalidate_running_jobs(new_gen)

        # Get signals and state
        signals = self._get_signals() if self._get_signals else {}
        context = self._get_system_state() if self._get_system_state else {}
        context["current_generation"] = self._current_generation

        # Scheduling tick
        admitted_jobs = self._scheduler.tick(signals, context)

        # Persist admitted jobs
        if self._persist_job:
            for job in admitted_jobs:
                self._persist_job(job)

        # Execute queued jobs
        self._execute_queued_jobs()

        # Clean up completed
        self._cleanup_completed()

    def _execution_loop(self) -> None:
        """Main execution loop."""
        while self._running and not self._shutdown_event.is_set():
            try:
                self.run_once()
            except Exception:
                # Log but continue
                time.sleep(1.0)

            # §10.63 R2: bound the loop to the scheduler's configured tick
            # cadence — previously the loop free-spun, paying a fresh PG
            # health connect + ledger parse per iteration (the dominant
            # share of measured idle CPU). Interruptible by shutdown.
            self._shutdown_event.wait(
                self.config.scheduler_config.tick_interval_seconds
            )

    def _execute_queued_jobs(self) -> None:
        """Execute jobs from the queue."""
        with self._lock:
            # Check concurrency limit
            if len(self._executing) >= self.config.max_concurrent_executions:
                return

            # Get next job
            result = self._scheduler.get_next_job()
            if not result:
                return

            job, action = result

            # Check if we have executor
            executor = self._executors.get(job.engine)
            if not executor:
                # No executor, mark failed
                job = job.with_status(MaintenanceJobStatus.FAILED)
                job = job.with_result("", "NO_EXECUTOR")
                self._scheduler.complete_job(job.job_id, job.status, "", "NO_EXECUTOR")
                if self._persist_job:
                    self._persist_job(job)
                return

            # Start execution in thread
            thread = threading.Thread(
                target=self._execute_job,
                args=(job, action, executor),
                name=f"maint-{job.action_id[:20]}",
                daemon=True,
            )
            self._executing[job.job_id] = (job, action, thread)
            thread.start()

    def _execute_job(
        self,
        job: MaintenanceJob,
        action: MaintenanceAction,
        executor: Callable,
    ) -> None:
        """Execute a single maintenance job."""
        try:
            # Update job to RUNNING
            running_job = job.with_status(MaintenanceJobStatus.RUNNING)
            if self._persist_job:
                self._persist_job(running_job)

            # Execute action
            before_state = dict(job.before_state)
            after_state = executor(job, action, before_state)

            # Verify
            verify_job = running_job.with_status(MaintenanceJobStatus.VERIFYING)
            if self._persist_job:
                self._persist_job(verify_job)

            verified, reason = verify_action(action, before_state, after_state, {})

            if verified:
                final_job = running_job.with_result("SUCCESS", "", after_state)
                final_job = final_job.with_status(MaintenanceJobStatus.SUCCEEDED)
            else:
                final_job = running_job.with_result("VERIFICATION_FAILED", reason, after_state)
                final_job = final_job.with_status(MaintenanceJobStatus.FAILED)

        except Exception as exc:
            final_job = job.with_result("EXECUTION_ERROR", str(exc))
            final_job = final_job.with_status(MaintenanceJobStatus.FAILED)

        # Persist final state
        if self._persist_job:
            self._persist_job(final_job)

        # Complete in scheduler
        self._scheduler.complete_job(
            final_job.job_id,
            final_job.status,
            final_job.result_code,
            final_job.error_code,
            final_job.after_state,
        )

        # Release lease
        lease_scope = self._get_lease_scope(job)
        release_lease(lease_scope, str(job.job_id))

    def _revalidate_running_jobs(self, new_generation: int) -> None:
        """Revalidate running jobs after generation change."""
        with self._lock:
            for job_id, (job, action, thread) in list(self._executing.items()):
                if job.generation != new_generation:
                    # Generation changed - job should revalidate
                    # For now, let it complete but mark for re-evaluation
                    pass

    def _cleanup_completed(self) -> None:
        """Clean up completed execution threads."""
        with self._lock:
            completed = []
            for job_id, (job, action, thread) in self._executing.items():
                if not thread.is_alive():
                    completed.append(job_id)
            for job_id in completed:
                del self._executing[job_id]

    def _wait_for_completion(self) -> None:
        """Wait for running jobs to complete during shutdown."""
        start = time.time()
        while self._executing and (time.time() - start) < self.config.max_shutdown_wait_seconds:
            time.sleep(0.5)
            self._cleanup_completed()

    def get_status(self) -> dict[str, Any]:
        """Get controller status."""
        with self._lock:
            return {
                "running": self._running,
                "paused": self._pause_event.is_set(),
                "generation": self._current_generation,
                "executing": len(self._executing),
                "scheduler": self._scheduler.get_queue_snapshot(),
                "budget": self._budget.snapshot(),
            }

    def cancel_job(
        self, job_id: UUID, reason: str = ""
    ) -> Optional[MaintenanceJob]:
        """Cancel a queued or running job and persist the CANCELLED row.

        Was a no-op surface: the C shadow cancels its mirror but the
        Python authoritative model never persisted the transition, so a
        recovered restart would resurrect the job.  Returns the cancelled
        job, or ``None`` when the id is unknown/already terminal
        (fail-closed, same rule as the C model).
        """
        cancelled = self._scheduler.cancel_job(job_id, reason=reason)
        if cancelled is not None and self._persist_job:
            self._persist_job(cancelled)
        return cancelled

    def trigger_maintenance_cycle(self) -> list[MaintenanceJob]:
        """Manually trigger a maintenance cycle (for testing/debugging)."""
        signals = self._get_signals() if self._get_signals else {}
        context = self._get_system_state() if self._get_system_state else {}
        context["current_generation"] = self._current_generation
        return self._scheduler.tick(signals, context)


def run_maintenance_cycle(
    config: ControllerConfig | None = None,
) -> MaintenanceController:
    """Create and start a maintenance controller."""
    controller = MaintenanceController(config)
    controller.start()
    return controller


__all__ = [
    "ControllerConfig",
    "MaintenanceController",
    "run_maintenance_cycle",
]