"""Startup & Shutdown Lifecycle Consolidation V1.

Consolidates the canonical lifecycle into a single model:

    Lifecycle states (A192/E167):
        UNINITIALIZED → INITIALIZING → READY → DRAINING → STOPPING → STOPPED
                       ↘ FAILED (from any state)

    Startup phases:
        BOOTSTRAP → CORE_INIT → PERSISTENCE_INIT →
        OPTIONAL_CAPABILITY_INIT → SERVICE_INIT → READY

    Shutdown (reverse dependency order):
        DRAINING → STOP_SERVICES → STOP_WORKERS →
        FLUSH_STATE → CLOSE_PERSISTENCE → STOPPED

Python is the sole runtime lifecycle authority.  TypeScript only
consumes typed readiness/health states.  C/C++ only provides native
capability lifecycle.  C# only provides Windows/.NET adapter lifecycle.
SQL only provides persistence readiness.

Rules:
    - No Python import-time DB connection, native load, model scan,
      worker/thread startup — use explicit initialize/start/stop/dispose.
    - Required capability failure → rollback completed phases.
    - Optional capability failure → degraded mode if A219 fallback complete.
    - Shutdown runs in reverse dependency order.
    - All workers/tasks/subprocesses have owner, cancel, join timeout.
    - Deadline/cancellation propagates across Python/native/C#.
    - C++/C# never own business shutdown policy.
    - Launcher stays thin — only process start + basic control.

Codex basis:
    A192/E167 — startup sovereign (max 3 capabilities)
    A191/E166 — dependency classification
    A194/E169 — readiness handoff
    A219 — Python fallback always preserved
    A204/A220 — C ABI boundary
    A205 — API boundary
    A211 — six-language canonical roles
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Canonical lifecycle states
# ---------------------------------------------------------------------------

class LifecycleState(str, Enum):
    """Canonical lifecycle states (A192/E167)."""
    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


# Valid state transitions
_VALID_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.UNINITIALIZED: frozenset({LifecycleState.INITIALIZING}),
    LifecycleState.INITIALIZING: frozenset({
        LifecycleState.READY,
        LifecycleState.FAILED,
    }),
    LifecycleState.READY: frozenset({
        LifecycleState.DRAINING,
        LifecycleState.FAILED,
    }),
    LifecycleState.DRAINING: frozenset({
        LifecycleState.STOPPING,
        LifecycleState.FAILED,
    }),
    LifecycleState.STOPPING: frozenset({
        LifecycleState.STOPPED,
        LifecycleState.FAILED,
    }),
    LifecycleState.STOPPED: frozenset(),   # terminal
    LifecycleState.FAILED: frozenset({     # can re-init after failure
        LifecycleState.UNINITIALIZED,
    }),
}


# ---------------------------------------------------------------------------
# Startup phases
# ---------------------------------------------------------------------------

class StartupPhase(str, Enum):
    """Canonical startup phases."""
    BOOTSTRAP = "BOOTSTRAP"
    CORE_INIT = "CORE_INIT"
    PERSISTENCE_INIT = "PERSISTENCE_INIT"
    OPTIONAL_CAPABILITY_INIT = "OPTIONAL_CAPABILITY_INIT"
    SERVICE_INIT = "SERVICE_INIT"
    READY = "READY"


STARTUP_PHASE_ORDER: tuple[StartupPhase, ...] = tuple(StartupPhase)


# ---------------------------------------------------------------------------
# Shutdown phases (reverse dependency order)
# ---------------------------------------------------------------------------

class ShutdownPhase(str, Enum):
    """Canonical shutdown phases (reverse dependency order)."""
    DRAINING = "DRAINING"
    STOP_SERVICES = "STOP_SERVICES"
    STOP_WORKERS = "STOP_WORKERS"
    FLUSH_STATE = "FLUSH_STATE"
    CLOSE_PERSISTENCE = "CLOSE_PERSISTENCE"
    STOPPED = "STOPPED"


SHUTDOWN_PHASE_ORDER: tuple[ShutdownPhase, ...] = tuple(ShutdownPhase)


# ---------------------------------------------------------------------------
# Capability criticality
# ---------------------------------------------------------------------------

class CapabilityCriticality(str, Enum):
    """Capability criticality classification."""
    REQUIRED = "REQUIRED"     # failure blocks startup → rollback
    OPTIONAL = "OPTIONAL"     # failure → degraded mode if A219 fallback complete


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------

class LifecycleStateError(RuntimeError):
    """Raised on invalid state transition."""


@dataclass
class LifecycleStateMachine:
    """Canonical lifecycle state machine.

    Python is the sole runtime lifecycle authority.  All transitions
    go through this state machine; no other language may change the
    lifecycle state directly.
    """

    _state: LifecycleState = LifecycleState.UNINITIALIZED
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _transition_log: list[tuple[LifecycleState, LifecycleState, float, str]] = field(
        default_factory=list, repr=False
    )

    @property
    def state(self) -> LifecycleState:
        with self._lock:
            return self._state

    @property
    def transition_log(self) -> list[tuple[LifecycleState, LifecycleState, float, str]]:
        with self._lock:
            return list(self._transition_log)

    def transition(
        self,
        target: LifecycleState,
        *,
        reason: str = "",
    ) -> LifecycleState:
        """Transition to a new state.

        Raises LifecycleStateError if the transition is invalid.
        """
        with self._lock:
            current = self._state
            valid = _VALID_TRANSITIONS.get(current, frozenset())
            if target not in valid:
                raise LifecycleStateError(
                    f"invalid transition: {current.value} → {target.value} "
                    f"(valid: {sorted(s.value for s in valid)})"
                )
            self._state = target
            self._transition_log.append(
                (current, target, time.monotonic(), reason)
            )
            return target

    def can_transition(self, target: LifecycleState) -> bool:
        """Check if a transition is valid without performing it."""
        with self._lock:
            valid = _VALID_TRANSITIONS.get(self._state, frozenset())
            return target in valid

    def reset(self) -> None:
        """Reset to UNINITIALIZED (only from FAILED or STOPPED)."""
        with self._lock:
            if self._state not in (LifecycleState.FAILED, LifecycleState.STOPPED):
                raise LifecycleStateError(
                    f"cannot reset from {self._state.value} "
                    f"(only from FAILED or STOPPED)"
                )
            self._state = LifecycleState.UNINITIALIZED


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityDeclaration:
    """A capability declaration for the lifecycle DAG.

    Each capability has:
        - identity (unique name)
        - criticality (REQUIRED or OPTIONAL)
        - phase (which startup phase initializes it)
        - dependencies (other capabilities it depends on)
        - initialize/start/stop/dispose handlers
        - fallback (for OPTIONAL: A219 fallback capability)
    """
    identity: str
    criticality: CapabilityCriticality
    phase: StartupPhase
    dependencies: tuple[str, ...] = ()
    fallback: str = ""  # A219 fallback capability identity (OPTIONAL only)

    # Lifecycle handlers (optional — may be None for declarative-only)
    initialize: Optional[Callable[[], None]] = None
    start: Optional[Callable[[], None]] = None
    stop: Optional[Callable[[], None]] = None
    dispose: Optional[Callable[[], None]] = None

    @property
    def is_required(self) -> bool:
        return self.criticality == CapabilityCriticality.REQUIRED

    @property
    def is_optional(self) -> bool:
        return self.criticality == CapabilityCriticality.OPTIONAL

    @property
    def has_fallback(self) -> bool:
        return bool(self.fallback)


# ---------------------------------------------------------------------------
# Lifecycle dependency DAG
# ---------------------------------------------------------------------------

class LifecycleDAGError(RuntimeError):
    """Raised on DAG construction or traversal errors."""


@dataclass
class LifecycleDependencyDAG:
    """Lifecycle dependency DAG with reverse-order shutdown.

    Capabilities are organized by startup phase.  Shutdown runs in
    reverse dependency order: capabilities with no dependents stop first.
    """

    capabilities: dict[str, CapabilityDeclaration] = field(default_factory=dict)

    def add(self, cap: CapabilityDeclaration) -> None:
        """Add a capability to the DAG."""
        if cap.identity in self.capabilities:
            raise LifecycleDAGError(f"duplicate capability: {cap.identity}")
        self.capabilities[cap.identity] = cap

    def is_acyclic(self) -> bool:
        """Check for cycles in the dependency graph."""
        graph: dict[str, list[str]] = {}
        for cap_id, cap in self.capabilities.items():
            graph.setdefault(cap_id, [])
            for dep in cap.dependencies:
                if dep not in self.capabilities:
                    continue  # missing dep — not a cycle
                graph.setdefault(dep, [])
                graph[cap_id].append(dep)

        visited: set[str] = set()
        in_stack: set[str] = set()

        def has_cycle(node: str) -> bool:
            if node in in_stack:
                return True
            if node in visited:
                return False
            visited.add(node)
            in_stack.add(node)
            for neighbor in graph.get(node, []):
                if has_cycle(neighbor):
                    return True
            in_stack.discard(node)
            return False

        return not any(has_cycle(n) for n in graph)

    def startup_order(self) -> list[str]:
        """Return capabilities in startup order (topological sort by phase)."""
        if not self.is_acyclic():
            raise LifecycleDAGError("cyclic dependency graph")

        # Group by phase, then topological sort within each phase
        by_phase: dict[StartupPhase, list[str]] = {}
        for cap_id, cap in self.capabilities.items():
            by_phase.setdefault(cap.phase, []).append(cap_id)

        result: list[str] = []
        for phase in STARTUP_PHASE_ORDER:
            phase_caps = by_phase.get(phase, [])
            # Topological sort within phase
            visited: set[str] = set()
            temp: set[str] = set()

            def visit(node: str) -> None:
                if node in temp:
                    return
                if node in visited:
                    return
                temp.add(node)
                for dep in self.capabilities[node].dependencies:
                    if dep in self.capabilities and self.capabilities[dep].phase == phase:
                        visit(dep)
                temp.discard(node)
                visited.add(node)
                result.append(node)

            for cap_id in phase_caps:
                visit(cap_id)

        return result

    def shutdown_order(self) -> list[str]:
        """Return capabilities in shutdown order (reverse of startup)."""
        return list(reversed(self.startup_order()))

    def required_capabilities(self) -> list[str]:
        """Return all REQUIRED capability identities."""
        return [
            cap_id for cap_id, cap in self.capabilities.items()
            if cap.is_required
        ]

    def optional_capabilities(self) -> list[str]:
        """Return all OPTIONAL capability identities."""
        return [
            cap_id for cap_id, cap in self.capabilities.items()
            if cap.is_optional
        ]

    def capabilities_by_phase(self, phase: StartupPhase) -> list[str]:
        """Return capabilities for a specific startup phase."""
        return [
            cap_id for cap_id, cap in self.capabilities.items()
            if cap.phase == phase
        ]


# ---------------------------------------------------------------------------
# Worker/task/subprocess handle
# ---------------------------------------------------------------------------

@dataclass
class WorkerHandle:
    """Handle for a worker/task/subprocess with owner, cancel, join timeout.

    Every worker/task/subprocess must have:
        - owner (which capability owns it)
        - cancel (cooperative cancellation)
        - join timeout (bounded wait for completion)
    """
    identity: str
    owner: str  # capability identity that owns this worker
    cancel_fn: Callable[[], None]
    join_fn: Callable[[float], bool]  # returns True if joined within timeout
    join_timeout_s: float = 30.0
    description: str = ""
    _cancelled: bool = field(default=False, repr=False)

    def cancel(self) -> None:
        """Cancel this worker cooperatively."""
        if not self._cancelled:
            self._cancelled = True
            self.cancel_fn()

    def join(self, timeout: float | None = None) -> bool:
        """Join this worker with a bounded timeout.

        Returns True if the worker completed within the timeout.
        """
        t = timeout if timeout is not None else self.join_timeout_s
        return self.join_fn(t)

    @property
    def cancelled(self) -> bool:
        return self._cancelled


class WorkerRegistry:
    """Registry of all workers/tasks/subprocesses.

    Ensures every worker has an owner, cancel, and join timeout.
    During shutdown, all workers are cancelled and joined in reverse
    dependency order.
    """

    def __init__(self) -> None:
        self._workers: dict[str, WorkerHandle] = {}
        self._lock = threading.Lock()

    def register(self, handle: WorkerHandle) -> None:
        with self._lock:
            if handle.identity in self._workers:
                raise ValueError(f"duplicate worker: {handle.identity}")
            self._workers[handle.identity] = handle

    def unregister(self, identity: str) -> None:
        with self._lock:
            self._workers.pop(identity, None)

    def get(self, identity: str) -> WorkerHandle | None:
        with self._lock:
            return self._workers.get(identity)

    def all_workers(self) -> list[WorkerHandle]:
        with self._lock:
            return list(self._workers.values())

    def workers_by_owner(self, owner: str) -> list[WorkerHandle]:
        with self._lock:
            return [
                w for w in self._workers.values()
                if w.owner == owner
            ]

    def cancel_all(self, join_timeout: float = 30.0) -> dict[str, bool]:
        """Cancel all workers and join with bounded timeout.

        Returns a dict mapping worker identity to join result
        (True if joined within timeout, False if timed out).
        """
        results: dict[str, bool] = {}
        with self._lock:
            workers = list(self._workers.values())

        # Cancel all first
        for w in workers:
            w.cancel()

        # Then join all
        for w in workers:
            results[w.identity] = w.join(join_timeout)

        return results

    def cancel_by_owner(self, owner: str, join_timeout: float = 30.0) -> dict[str, bool]:
        """Cancel and join all workers owned by a specific capability."""
        results: dict[str, bool] = {}
        workers = self.workers_by_owner(owner)
        for w in workers:
            w.cancel()
        for w in workers:
            results[w.identity] = w.join(join_timeout)
        return results


# ---------------------------------------------------------------------------
# Deadline/cancellation propagation
# ---------------------------------------------------------------------------

@dataclass
class DeadlineCancellation:
    """Deadline/cancellation that propagates across Python/native/C#.

    The deadline is set at the Python level and propagated to:
        - Native (C/C++) via the C ABI (cooperative polling)
        - C# via interop (cooperative polling)

    C++/C# never own business shutdown policy — they only check
    cancellation cooperatively.
    """
    deadline_monotonic: float  # absolute deadline (monotonic clock)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def remaining_seconds(self) -> float:
        """Remaining time until deadline."""
        return max(0.0, self.deadline_monotonic - time.monotonic())

    @property
    def expired(self) -> bool:
        """Check if the deadline has expired."""
        return time.monotonic() >= self.deadline_monotonic

    @property
    def cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        """Request cancellation."""
        self.cancel_event.set()

    def check(self) -> None:
        """Raise if cancelled or expired."""
        if self.cancelled:
            raise LifecycleCancelled("cancelled")
        if self.expired:
            raise LifecycleDeadlineExceeded("deadline exceeded")

    @classmethod
    def from_timeout(cls, timeout_s: float) -> DeadlineCancellation:
        """Create from a timeout (relative to now)."""
        return cls(deadline_monotonic=time.monotonic() + timeout_s)


class LifecycleCancelled(Exception):
    """Lifecycle was cancelled."""


class LifecycleDeadlineExceeded(Exception):
    """Lifecycle deadline was exceeded."""


# ---------------------------------------------------------------------------
# Timing evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhaseTiming:
    """Timing evidence for one startup/shutdown phase."""
    phase: str
    started_at: float   # monotonic
    completed_at: float  # monotonic
    duration_s: float
    success: bool
    error: str = ""


@dataclass
class LifecycleTimingEvidence:
    """Startup/shutdown timing evidence."""
    phases: list[PhaseTiming] = field(default_factory=list)
    total_duration_s: float = 0.0
    success: bool = True

    def add_phase(
        self,
        phase: str,
        started_at: float,
        completed_at: float,
        success: bool,
        error: str = "",
    ) -> None:
        """Record timing for one phase."""
        self.phases.append(PhaseTiming(
            phase=phase,
            started_at=started_at,
            completed_at=completed_at,
            duration_s=completed_at - started_at,
            success=success,
            error=error,
        ))
        if not success:
            self.success = False
        self.total_duration_s = sum(p.duration_s for p in self.phases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phases": [
                {
                    "phase": p.phase,
                    "duration_s": p.duration_s,
                    "success": p.success,
                    "error": p.error,
                }
                for p in self.phases
            ],
            "total_duration_s": self.total_duration_s,
            "success": self.success,
        }


# ---------------------------------------------------------------------------
# Lifecycle controller (Python as sole runtime lifecycle authority)
# ---------------------------------------------------------------------------

@dataclass
class LifecycleController:
    """Main lifecycle controller — Python is the sole runtime lifecycle authority.

    Coordinates:
        - State machine transitions
        - Startup phase execution (with rollback on REQUIRED failure)
        - Shutdown phase execution (reverse dependency order)
        - Worker registry (cancel + join timeout)
        - Deadline/cancellation propagation
        - Timing evidence collection

    This is NOT a second orchestration system — it consolidates the
    existing lifecycle concepts into a single canonical model.  The
    existing StartupSovereignExecutor and BootCore delegate to this
    controller for state management.
    """

    state_machine: LifecycleStateMachine = field(default_factory=LifecycleStateMachine)
    dag: LifecycleDependencyDAG = field(default_factory=LifecycleDependencyDAG)
    workers: WorkerRegistry = field(default_factory=WorkerRegistry)
    startup_timing: LifecycleTimingEvidence = field(default_factory=LifecycleTimingEvidence)
    shutdown_timing: LifecycleTimingEvidence = field(default_factory=LifecycleTimingEvidence)
    _completed_phases: list[StartupPhase] = field(default_factory=list, repr=False)
    _activated_capabilities: list[str] = field(default_factory=list, repr=False)
    _degraded_capabilities: set[str] = field(default_factory=set, repr=False)

    @property
    def state(self) -> LifecycleState:
        return self.state_machine.state

    @property
    def completed_phases(self) -> list[StartupPhase]:
        return list(self._completed_phases)

    @property
    def activated_capabilities(self) -> list[str]:
        return list(self._activated_capabilities)

    @property
    def degraded_capabilities(self) -> set[str]:
        return set(self._degraded_capabilities)

    def add_capability(self, cap: CapabilityDeclaration) -> None:
        """Add a capability to the lifecycle DAG."""
        self.dag.add(cap)

    def register_worker(self, handle: WorkerHandle) -> None:
        """Register a worker/task/subprocess."""
        self.workers.register(handle)

    def startup(
        self,
        *,
        deadline: DeadlineCancellation | None = None,
    ) -> LifecycleTimingEvidence:
        """Execute the startup sequence.

        Phases run in order: BOOTSTRAP → CORE_INIT → PERSISTENCE_INIT →
        OPTIONAL_CAPABILITY_INIT → SERVICE_INIT → READY.

        REQUIRED capability failure → rollback completed phases.
        OPTIONAL capability failure → degraded mode if A219 fallback complete.
        """
        self.state_machine.transition(LifecycleState.INITIALIZING, reason="startup")

        for phase in STARTUP_PHASE_ORDER:
            phase_start = time.monotonic()
            success = True
            error = ""

            try:
                if deadline:
                    deadline.check()
                self._execute_phase(phase, deadline)
                self._completed_phases.append(phase)
            except LifecycleCancelled as e:
                success = False
                error = str(e)
                self._handle_failure(phase, error, is_cancel=True)
            except LifecycleDeadlineExceeded as e:
                success = False
                error = str(e)
                self._handle_failure(phase, error, is_deadline=True)
            except Exception as e:
                success = False
                error = str(e)
                self._handle_failure(phase, error)

            self.startup_timing.add_phase(
                phase.value, phase_start, time.monotonic(), success, error,
            )

            if not success:
                break

        if self.startup_timing.success:
            self.state_machine.transition(LifecycleState.READY, reason="startup complete")
        else:
            self.state_machine.transition(LifecycleState.FAILED, reason="startup failed")

        return self.startup_timing

    def shutdown(
        self,
        *,
        deadline: DeadlineCancellation | None = None,
        join_timeout: float = 30.0,
    ) -> LifecycleTimingEvidence:
        """Execute the shutdown sequence.

        Phases run in reverse dependency order:
        DRAINING → STOP_SERVICES → STOP_WORKERS →
        FLUSH_STATE → CLOSE_PERSISTENCE → STOPPED.
        """
        if self.state_machine.state == LifecycleState.READY:
            self.state_machine.transition(LifecycleState.DRAINING, reason="shutdown")
        elif self.state_machine.state == LifecycleState.FAILED:
            # Can still shutdown from FAILED
            self.state_machine.transition(LifecycleState.DRAINING, reason="shutdown from failed")
        elif self.state_machine.state == LifecycleState.UNINITIALIZED:
            # Nothing to shutdown
            return self.shutdown_timing

        for phase in SHUTDOWN_PHASE_ORDER:
            phase_start = time.monotonic()
            success = True
            error = ""

            try:
                if deadline:
                    deadline.check()
                self._execute_shutdown_phase(phase, deadline, join_timeout)
            except LifecycleCancelled as e:
                success = False
                error = str(e)
            except LifecycleDeadlineExceeded as e:
                success = False
                error = str(e)
            except Exception as e:
                success = False
                error = str(e)

            self.shutdown_timing.add_phase(
                phase.value, phase_start, time.monotonic(), success, error,
            )

            if not success:
                # Continue shutdown even on failure — best effort
                pass

        # Transition to STOPPED (or FAILED if shutdown had errors)
        if self.shutdown_timing.success:
            self.state_machine.transition(LifecycleState.STOPPING, reason="shutdown complete")
            self.state_machine.transition(LifecycleState.STOPPED, reason="stopped")
        else:
            # If we're in DRAINING, go to STOPPING then STOPPED anyway
            if self.state_machine.state == LifecycleState.DRAINING:
                self.state_machine.transition(LifecycleState.STOPPING, reason="shutdown with errors")
            self.state_machine.transition(LifecycleState.STOPPED, reason="stopped")

        return self.shutdown_timing

    def _execute_phase(
        self,
        phase: StartupPhase,
        deadline: DeadlineCancellation | None,
    ) -> None:
        """Execute one startup phase."""
        cap_ids = self.dag.capabilities_by_phase(phase)

        for cap_id in cap_ids:
            cap = self.dag.capabilities[cap_id]
            if deadline:
                deadline.check()

            try:
                if cap.initialize:
                    cap.initialize()
                if cap.start:
                    cap.start()
                self._activated_capabilities.append(cap_id)
            except Exception as e:
                if cap.is_required:
                    raise
                elif cap.is_optional:
                    # Optional failure → degraded mode if A219 fallback complete
                    if cap.has_fallback:
                        self._degraded_capabilities.add(cap_id)
                        # Fallback capability should already be activated
                    else:
                        # No fallback — still degraded but no fallback path
                        self._degraded_capabilities.add(cap_id)

    def _execute_shutdown_phase(
        self,
        phase: ShutdownPhase,
        deadline: DeadlineCancellation | None,
        join_timeout: float,
    ) -> None:
        """Execute one shutdown phase."""
        if phase == ShutdownPhase.DRAINING:
            # Signal all workers to stop accepting new work
            pass  # workers check state via deadline/cancellation
        elif phase == ShutdownPhase.STOP_SERVICES:
            # Stop services in reverse dependency order
            for cap_id in reversed(self._activated_capabilities):
                cap = self.dag.capabilities.get(cap_id)
                if cap and cap.stop:
                    try:
                        cap.stop()
                    except Exception:
                        pass  # best effort during shutdown
        elif phase == ShutdownPhase.STOP_WORKERS:
            # Cancel and join all workers
            self.workers.cancel_all(join_timeout=join_timeout)
        elif phase == ShutdownPhase.FLUSH_STATE:
            # Flush state for activated capabilities
            for cap_id in reversed(self._activated_capabilities):
                cap = self.dag.capabilities.get(cap_id)
                if cap and cap.dispose:
                    try:
                        cap.dispose()
                    except Exception:
                        pass  # best effort during shutdown
        elif phase == ShutdownPhase.CLOSE_PERSISTENCE:
            # Close persistence (handled by persistence layer)
            pass
        elif phase == ShutdownPhase.STOPPED:
            pass  # terminal

    def _handle_failure(
        self,
        phase: StartupPhase,
        error: str,
        *,
        is_cancel: bool = False,
        is_deadline: bool = False,
    ) -> None:
        """Handle a startup failure — rollback completed phases.

        Does NOT transition to FAILED — the caller (startup) handles
        the final state transition.
        """
        # Rollback in reverse order
        for cap_id in reversed(self._activated_capabilities):
            cap = self.dag.capabilities.get(cap_id)
            if cap and cap.stop:
                try:
                    cap.stop()
                except Exception:
                    pass  # best effort during rollback
            if cap and cap.dispose:
                try:
                    cap.dispose()
                except Exception:
                    pass  # best effort during rollback

        # Cancel all workers
        self.workers.cancel_all(join_timeout=5.0)


# ---------------------------------------------------------------------------
# Launcher contract (thin launcher)
# ---------------------------------------------------------------------------

LAUNCHER_DUTIES: tuple[str, ...] = (
    "start-process",
    "basic-process-control",
    "subscribe-to-lifecycle-events",
    "display-readiness-state",
)

LAUNCHER_FORBIDDEN: tuple[str, ...] = (
    "lifecycle-decisions",
    "dependency-checks",
    "governance-checks",
    "direct-database-start",
    "direct-model-start",
    "owning-lifecycle-state",
    "blocking-ui-until-ready",
    "second-lifecycle-controller",
)


def is_launcher_action_allowed(action: str) -> bool:
    """Check if an action is allowed for the thin launcher."""
    return action in LAUNCHER_DUTIES and action not in LAUNCHER_FORBIDDEN


__all__ = [
    "LifecycleState",
    "StartupPhase",
    "ShutdownPhase",
    "STARTUP_PHASE_ORDER",
    "SHUTDOWN_PHASE_ORDER",
    "CapabilityCriticality",
    "LifecycleStateError",
    "LifecycleStateMachine",
    "CapabilityDeclaration",
    "LifecycleDAGError",
    "LifecycleDependencyDAG",
    "WorkerHandle",
    "WorkerRegistry",
    "DeadlineCancellation",
    "LifecycleCancelled",
    "LifecycleDeadlineExceeded",
    "PhaseTiming",
    "LifecycleTimingEvidence",
    "LifecycleController",
    "LAUNCHER_DUTIES",
    "LAUNCHER_FORBIDDEN",
    "is_launcher_action_allowed",
]
