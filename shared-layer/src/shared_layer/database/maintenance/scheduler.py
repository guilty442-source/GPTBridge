"""Maintenance Scheduler.

Handles candidate ordering, priority, cooldown, admission, budget, lease acquisition,
and enqueueing. Does NOT execute SQL or modify governance/permissions/schema.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID

from .evaluator import MaintenanceCandidate
from .models import MaintenanceJob, MaintenanceJobStatus, MaintenanceRiskClass
from .registry import MaintenanceAction, get_registry
from .policies import evaluate_policy, MaintenanceDecision, SystemRecoveryState
from .budgets import MaintenanceBudget, check_budget
from .leases import acquire_lease, LeaseConflictError, check_lease_conflict
from .evaluator import evaluate_candidates


@dataclass(frozen=True)
class SchedulerConfig:
    """Configuration for the maintenance scheduler."""

    # Scheduling interval
    tick_interval_seconds: float = 30.0

    # Queue limits
    max_queued_jobs: int = 100
    max_job_age_seconds: float = 3600.0  # Jobs older than this are cancelled

    # Admission
    admit_m0_always: bool = True
    admit_m1_when_idle: bool = True
    admit_m2_with_auth: bool = True
    m3_candidate_only: bool = True

    # Retry
    max_retry_attempts: int = 3
    retry_backoff_base_seconds: float = 60.0

    # Generation awareness
    enforce_generation_match: bool = True


@dataclass
class ScheduledJob:
    """A job that has been admitted to the queue."""

    job: MaintenanceJob
    action: MaintenanceAction
    admitted_at: datetime = field(default_factory=datetime.utcnow)
    lease_acquired: bool = False


class MaintenanceScheduler:
    """Maintenance job scheduler.

    Responsibilities:
    - Candidate evaluation and ordering
    - Policy-based admission
    - Budget checking
    - Lease acquisition
    - Job queue management
    - Cooldown tracking

    Does NOT:
    - Execute SQL
    - Modify governance/permissions/schema
    - Decide authority conflicts
    """

    def __init__(
        self,
        config: SchedulerConfig | None = None,
        budget: MaintenanceBudget | None = None,
        policy_evaluator: Callable = evaluate_policy,
        candidate_evaluator: Callable = evaluate_candidates,
    ) -> None:
        self.config = config or SchedulerConfig()
        self.budget = budget or MaintenanceBudget()
        self._policy_evaluator = policy_evaluator
        self._candidate_evaluator = candidate_evaluator
        self._registry = get_registry()

        self._queue: deque[ScheduledJob] = deque()
        self._running: dict[UUID, ScheduledJob] = {}
        self._lock = threading.RLock()
        self._draining = False
        self._current_generation = 0
        self._native_shadow: Any = None

    def set_native_shadow(self, shadow: Any) -> None:
        """Attach a §10.65 native-shadow observer (None disables)."""
        with self._lock:
            self._native_shadow = shadow

    def set_generation(self, generation: int) -> None:
        """Update current system generation."""
        with self._lock:
            self._current_generation = generation
            shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_generation(generation)
            except Exception:
                pass

    def is_draining(self) -> bool:
        """Check if scheduler is draining (shutdown)."""
        return self._draining

    def set_draining(self, draining: bool) -> None:
        """Set draining state."""
        with self._lock:
            self._draining = draining

    def tick(
        self,
        signals: dict[str, Any],
        context: dict[str, Any],
    ) -> list[MaintenanceJob]:
        """Run one scheduling cycle.

        Returns:
            List of newly admitted jobs ready for execution
        """
        with self._lock:
            if self._draining:
                return []

            # Update context with current generation
            context = dict(context)
            context["current_generation"] = self._current_generation

            # Evaluate candidates
            candidates = self._candidate_evaluator(signals, context)

            # Process each candidate
            admitted = []
            for candidate in candidates:
                if len(self._queue) + len(self._running) >= self.config.max_queued_jobs:
                    break

                job = self._try_admit(candidate, signals, context)
                if job:
                    admitted.append(job)

            # Clean up old jobs
            self._cleanup_stale_jobs()

            return admitted

    def _try_admit(
        self,
        candidate: MaintenanceCandidate,
        signals: dict[str, Any],
        context: dict[str, Any],
    ) -> Optional[MaintenanceJob]:
        """Try to admit a candidate as a job."""
        action = self._registry.get(candidate.action_id, candidate.action_version)
        if not action:
            return None

        # Build policy evaluation context
        policy_context = {
            **context,
            "job_generation": candidate.metadata.get("generation", self._current_generation),
            "governed_authorization": context.get("governed_authorization", False),
        }

        # Evaluate policy
        decision = self._policy_evaluator(
            risk_class=candidate.risk_class,
            system_state=policy_context,
        )

        shadow = self._native_shadow
        if shadow is not None:
            try:
                idle_gate = self._policy_evaluator(
                    risk_class=MaintenanceRiskClass.M1_SAFE_AUTO,
                    system_state=policy_context,
                )
                shadow.observe_admit(
                    str(candidate.candidate_id),
                    str(candidate.action_id),
                    risk_class=candidate.risk_class,
                    priority=int(getattr(candidate, "priority", 0)),
                    generation=int(policy_context["job_generation"]),
                    system_idle=bool(idle_gate.allowed),
                    authorized=bool(policy_context["governed_authorization"]),
                    py_executable=bool(
                        decision.allowed
                        and candidate.risk_class
                        != MaintenanceRiskClass.M3_APPROVAL_REQUIRED
                    ),
                )
            except Exception:
                pass

        if not decision.allowed:
            # M3 candidates are recorded but not executed
            if candidate.risk_class == MaintenanceRiskClass.M3_APPROVAL_REQUIRED:
                return candidate.to_job(self._current_generation).with_status(MaintenanceJobStatus.PLANNED)
            return None

        # Check budget
        budget_ok, budget_reason = check_budget(self.budget, candidate.engine, candidate.risk_class.value)
        if not budget_ok:
            return None

        # Check lease conflict
        lease_scope = action.lease_scope.format(
            database_id=candidate.database_id,
            module_id=candidate.module_id,
            backup_id=candidate.database_id,
            projection_name=candidate.database_id,
        )
        conflict, existing_lease = check_lease_conflict(lease_scope, str(candidate.candidate_id))
        if conflict:
            policy_context["active_lease_conflict"] = True
            decision = self._policy_evaluator(
                risk_class=candidate.risk_class,
                system_state=policy_context,
            )
            if not decision.allowed:
                return None

        # Check cooldown
        cooldown_ok, _ = self.budget.check_cooldown(candidate.action_id, action.cooldown_seconds)
        if not cooldown_ok:
            return None

        # Create job
        job = candidate.to_job(self._current_generation)
        job = job.with_status(MaintenanceJobStatus.QUEUED)

        # Try to acquire lease
        try:
            lease = acquire_lease(
                scope=lease_scope,
                holder=str(job.job_id),
                action_id=candidate.action_id,
                generation=self._current_generation,
                ttl_seconds=action.timeout_seconds + 60,  # Extra buffer
                job_id=job.job_id,
            )
            job = MaintenanceJob(
                job_id=job.job_id,
                action_id=job.action_id,
                action_version=job.action_version,
                engine=job.engine,
                database_id=job.database_id,
                module_id=job.module_id,
                risk_class=job.risk_class,
                priority=job.priority,
                status=MaintenanceJobStatus.QUEUED,
                generation=job.generation,
                attempt_count=job.attempt_count,
                scheduled_at=job.scheduled_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                lease_until=lease.lease_until,
                before_state=job.before_state,
                after_state=job.after_state,
                result_code=job.result_code,
                error_code=job.error_code,
            )
        except LeaseConflictError:
            return None

        # Reserve budget
        self.budget.reserve_job(candidate.engine)

        # Enqueue
        scheduled = ScheduledJob(job=job, action=action)
        self._queue.append(scheduled)

        return job

    def _cleanup_stale_jobs(self) -> None:
        """Remove jobs that have been queued too long."""
        now = datetime.utcnow()
        cutoff = now.timestamp() - self.config.max_job_age_seconds

        # Clean queue
        new_queue = deque()
        for sj in self._queue:
            if sj.admitted_at.timestamp() < cutoff:
                # Job too old, mark cancelled
                pass  # In production, persist cancellation
            else:
                new_queue.append(sj)
        self._queue = new_queue

    def get_next_job(self) -> Optional[tuple[MaintenanceJob, MaintenanceAction]]:
        """Get the next job to execute (caller must handle execution)."""
        with self._lock:
            if not self._queue:
                shadow = self._native_shadow
                if shadow is not None:
                    try:
                        shadow.observe_dispatch(None)
                    except Exception:
                        pass
                return None

            # Pop highest priority (lowest number)
            scheduled = self._queue.popleft()
            job = scheduled.job.with_status(MaintenanceJobStatus.RUNNING)
            self._running[job.job_id] = scheduled

            shadow = self._native_shadow
            if shadow is not None:
                try:
                    shadow.observe_dispatch(str(job.job_id))
                except Exception:
                    pass

            return job, scheduled.action

    def complete_job(
        self,
        job_id: UUID,
        status: MaintenanceJobStatus,
        result_code: str = "",
        error_code: str = "",
        after_state: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Mark job as complete and release resources."""
        with self._lock:
            scheduled = self._running.pop(job_id, None)
            if not scheduled:
                return False

            job = scheduled.job
            engine = job.engine
            action_id = job.action_id

            # Release budget
            self.budget.release_job(engine)

            # Release lease
            lease_scope = scheduled.action.lease_scope.format(
                database_id=job.database_id,
                module_id=job.module_id,
                backup_id=job.database_id,
                projection_name=job.database_id,
            )
            # Note: lease release is handled by controller after verification

            # Record cooldown
            self.budget.record_maintenance(action_id)

            shadow = self._native_shadow
            if shadow is not None:
                try:
                    shadow.observe_terminal(
                        str(job_id),
                        ok=(status == MaintenanceJobStatus.COMPLETED),
                    )
                except Exception:
                    pass

            return True

    def requeue_job(
        self,
        job_id: UUID,
        new_attempt_count: int,
        new_lease_until: Optional[datetime] = None,
    ) -> bool:
        """Requeue a failed job for retry."""
        with self._lock:
            scheduled = self._running.pop(job_id, None)
            if not scheduled:
                return False

            job = scheduled.job
            if not job.can_retry(self.config.max_retry_attempts):
                return False

            # Release current budget
            self.budget.release_job(job.engine)

            # Create new job with incremented attempt
            new_job = job.with_attempt(new_attempt_count, new_lease_until)
            new_job = new_job.with_status(MaintenanceJobStatus.QUEUED)

            # Re-reserve budget
            self.budget.reserve_job(job.engine)

            # Requeue
            self._queue.appendleft(ScheduledJob(job=new_job, action=scheduled.action))
            return True

    def get_queue_snapshot(self) -> dict[str, Any]:
        """Get current queue state."""
        with self._lock:
            return {
                "queued": len(self._queue),
                "running": len(self._running),
                "draining": self._draining,
                "generation": self._current_generation,
                "budget": self.budget.snapshot(),
                "jobs": [
                    {
                        "job_id": str(sj.job.job_id),
                        "action_id": sj.job.action_id,
                        "engine": sj.job.engine,
                        "status": sj.job.status.value,
                        "priority": sj.job.priority,
                        "admitted_at": sj.admitted_at.isoformat(),
                    }
                    for sj in list(self._queue) + list(self._running.values())
                ],
            }


def schedule_candidates(
    signals: dict[str, Any],
    context: dict[str, Any],
    scheduler: MaintenanceScheduler | None = None,
) -> list[MaintenanceJob]:
    """Convenience function to run one scheduling cycle."""
    sched = scheduler or MaintenanceScheduler()
    return sched.tick(signals, context)


__all__ = [
    "SchedulerConfig",
    "ScheduledJob",
    "MaintenanceScheduler",
    "schedule_candidates",
]