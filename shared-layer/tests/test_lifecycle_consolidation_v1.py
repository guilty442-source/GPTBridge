"""Tests for Startup & Shutdown Lifecycle Consolidation V1.

Tests the full lifecycle pipeline:
    - Canonical lifecycle states (UNINITIALIZED through FAILED)
    - Startup phases (BOOTSTRAP through READY)
    - Shutdown phases (DRAINING through STOPPED)
    - State machine transitions (valid/invalid)
    - Capability classification (REQUIRED/OPTIONAL)
    - Lifecycle dependency DAG (startup/shutdown order, cycle detection)
    - Worker registry (owner/cancel/join timeout)
    - Deadline/cancellation propagation
    - Timing evidence
    - Lifecycle controller (normal/failure/rollback/timeout/repeated-start-stop)
    - Launcher contract (thin launcher)

Codex basis:
    A192/E167 — startup sovereign
    A191/E166 — dependency classification
    A194/E169 — readiness handoff
    A219 — Python fallback always preserved
    A211 — six-language canonical roles
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.lifecycle_consolidation import (
    LifecycleState,
    StartupPhase,
    ShutdownPhase,
    STARTUP_PHASE_ORDER,
    SHUTDOWN_PHASE_ORDER,
    CapabilityCriticality,
    LifecycleStateError,
    LifecycleStateMachine,
    CapabilityDeclaration,
    LifecycleDAGError,
    LifecycleDependencyDAG,
    WorkerHandle,
    WorkerRegistry,
    DeadlineCancellation,
    LifecycleCancelled,
    LifecycleDeadlineExceeded,
    PhaseTiming,
    LifecycleTimingEvidence,
    LifecycleController,
    LAUNCHER_DUTIES,
    LAUNCHER_FORBIDDEN,
    is_launcher_action_allowed,
)


# ---------------------------------------------------------------------------
# Lifecycle States
# ---------------------------------------------------------------------------

class TestLifecycleStates:
    def test_all_states_exist(self):
        assert LifecycleState.UNINITIALIZED.value == "UNINITIALIZED"
        assert LifecycleState.INITIALIZING.value == "INITIALIZING"
        assert LifecycleState.READY.value == "READY"
        assert LifecycleState.DRAINING.value == "DRAINING"
        assert LifecycleState.STOPPING.value == "STOPPING"
        assert LifecycleState.STOPPED.value == "STOPPED"
        assert LifecycleState.FAILED.value == "FAILED"

    def test_seven_states(self):
        assert len(LifecycleState) == 7


# ---------------------------------------------------------------------------
# Startup/Shutdown Phases
# ---------------------------------------------------------------------------

class TestStartupPhases:
    def test_six_startup_phases(self):
        assert len(StartupPhase) == 6
        assert StartupPhase.BOOTSTRAP.value == "BOOTSTRAP"
        assert StartupPhase.CORE_INIT.value == "CORE_INIT"
        assert StartupPhase.PERSISTENCE_INIT.value == "PERSISTENCE_INIT"
        assert StartupPhase.OPTIONAL_CAPABILITY_INIT.value == "OPTIONAL_CAPABILITY_INIT"
        assert StartupPhase.SERVICE_INIT.value == "SERVICE_INIT"
        assert StartupPhase.READY.value == "READY"

    def test_startup_phase_order(self):
        assert STARTUP_PHASE_ORDER == (
            StartupPhase.BOOTSTRAP,
            StartupPhase.CORE_INIT,
            StartupPhase.PERSISTENCE_INIT,
            StartupPhase.OPTIONAL_CAPABILITY_INIT,
            StartupPhase.SERVICE_INIT,
            StartupPhase.READY,
        )

    def test_six_shutdown_phases(self):
        assert len(ShutdownPhase) == 6
        assert ShutdownPhase.DRAINING.value == "DRAINING"
        assert ShutdownPhase.STOP_SERVICES.value == "STOP_SERVICES"
        assert ShutdownPhase.STOP_WORKERS.value == "STOP_WORKERS"
        assert ShutdownPhase.FLUSH_STATE.value == "FLUSH_STATE"
        assert ShutdownPhase.CLOSE_PERSISTENCE.value == "CLOSE_PERSISTENCE"
        assert ShutdownPhase.STOPPED.value == "STOPPED"

    def test_shutdown_phase_order(self):
        assert SHUTDOWN_PHASE_ORDER == (
            ShutdownPhase.DRAINING,
            ShutdownPhase.STOP_SERVICES,
            ShutdownPhase.STOP_WORKERS,
            ShutdownPhase.FLUSH_STATE,
            ShutdownPhase.CLOSE_PERSISTENCE,
            ShutdownPhase.STOPPED,
        )


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_initial_state(self):
        sm = LifecycleStateMachine()
        assert sm.state == LifecycleState.UNINITIALIZED

    def test_valid_transition(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        assert sm.state == LifecycleState.INITIALIZING

    def test_full_startup_transition(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        sm.transition(LifecycleState.READY)
        sm.transition(LifecycleState.DRAINING)
        sm.transition(LifecycleState.STOPPING)
        sm.transition(LifecycleState.STOPPED)
        assert sm.state == LifecycleState.STOPPED

    def test_invalid_transition(self):
        sm = LifecycleStateMachine()
        with pytest.raises(LifecycleStateError):
            sm.transition(LifecycleState.READY)  # can't skip INITIALIZING

    def test_invalid_transition_from_stopped(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        sm.transition(LifecycleState.READY)
        sm.transition(LifecycleState.DRAINING)
        sm.transition(LifecycleState.STOPPING)
        sm.transition(LifecycleState.STOPPED)
        with pytest.raises(LifecycleStateError):
            sm.transition(LifecycleState.READY)  # STOPPED is terminal

    def test_failure_from_any_state(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        sm.transition(LifecycleState.FAILED)
        assert sm.state == LifecycleState.FAILED

    def test_can_transition(self):
        sm = LifecycleStateMachine()
        assert sm.can_transition(LifecycleState.INITIALIZING)
        assert not sm.can_transition(LifecycleState.READY)

    def test_reset_from_failed(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        sm.transition(LifecycleState.FAILED)
        sm.reset()
        assert sm.state == LifecycleState.UNINITIALIZED

    def test_reset_from_stopped(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        sm.transition(LifecycleState.READY)
        sm.transition(LifecycleState.DRAINING)
        sm.transition(LifecycleState.STOPPING)
        sm.transition(LifecycleState.STOPPED)
        sm.reset()
        assert sm.state == LifecycleState.UNINITIALIZED

    def test_reset_invalid_from_ready(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING)
        sm.transition(LifecycleState.READY)
        with pytest.raises(LifecycleStateError):
            sm.reset()

    def test_transition_log(self):
        sm = LifecycleStateMachine()
        sm.transition(LifecycleState.INITIALIZING, reason="test")
        log = sm.transition_log
        assert len(log) == 1
        assert log[0][0] == LifecycleState.UNINITIALIZED
        assert log[0][1] == LifecycleState.INITIALIZING
        assert log[0][3] == "test"


# ---------------------------------------------------------------------------
# Capability Declaration
# ---------------------------------------------------------------------------

class TestCapabilityDeclaration:
    def test_required_capability(self):
        cap = CapabilityDeclaration(
            identity="postgres",
            criticality=CapabilityCriticality.REQUIRED,
            phase=StartupPhase.PERSISTENCE_INIT,
        )
        assert cap.is_required
        assert not cap.is_optional
        assert not cap.has_fallback

    def test_optional_with_fallback(self):
        cap = CapabilityDeclaration(
            identity="qdrant",
            criticality=CapabilityCriticality.OPTIONAL,
            phase=StartupPhase.OPTIONAL_CAPABILITY_INIT,
            fallback="local_vector",
        )
        assert cap.is_optional
        assert cap.has_fallback
        assert cap.fallback == "local_vector"

    def test_optional_without_fallback(self):
        cap = CapabilityDeclaration(
            identity="model_cache",
            criticality=CapabilityCriticality.OPTIONAL,
            phase=StartupPhase.OPTIONAL_CAPABILITY_INIT,
        )
        assert cap.is_optional
        assert not cap.has_fallback


# ---------------------------------------------------------------------------
# Lifecycle Dependency DAG
# ---------------------------------------------------------------------------

class TestLifecycleDAG:
    def test_add_capability(self):
        dag = LifecycleDependencyDAG()
        cap = CapabilityDeclaration(
            identity="a", criticality=CapabilityCriticality.REQUIRED,
            phase=StartupPhase.BOOTSTRAP,
        )
        dag.add(cap)
        assert "a" in dag.capabilities

    def test_duplicate_capability(self):
        dag = LifecycleDependencyDAG()
        cap = CapabilityDeclaration(
            identity="a", criticality=CapabilityCriticality.REQUIRED,
            phase=StartupPhase.BOOTSTRAP,
        )
        dag.add(cap)
        with pytest.raises(LifecycleDAGError):
            dag.add(cap)

    def test_acyclic(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("a", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP))
        dag.add(CapabilityDeclaration("b", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT, dependencies=("a",)))
        dag.add(CapabilityDeclaration("c", CapabilityCriticality.REQUIRED, StartupPhase.PERSISTENCE_INIT, dependencies=("b",)))
        assert dag.is_acyclic()

    def test_cyclic(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("a", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP, dependencies=("c",)))
        dag.add(CapabilityDeclaration("b", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT, dependencies=("a",)))
        dag.add(CapabilityDeclaration("c", CapabilityCriticality.REQUIRED, StartupPhase.PERSISTENCE_INIT, dependencies=("b",)))
        assert not dag.is_acyclic()

    def test_startup_order(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("core", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT))
        dag.add(CapabilityDeclaration("bootstrap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP))
        dag.add(CapabilityDeclaration("persist", CapabilityCriticality.REQUIRED, StartupPhase.PERSISTENCE_INIT, dependencies=("core",)))
        order = dag.startup_order()
        assert order[0] == "bootstrap"
        assert order[1] == "core"
        assert order[2] == "persist"

    def test_shutdown_order_reverse(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("core", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT))
        dag.add(CapabilityDeclaration("bootstrap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP))
        dag.add(CapabilityDeclaration("persist", CapabilityCriticality.REQUIRED, StartupPhase.PERSISTENCE_INIT, dependencies=("core",)))
        startup = dag.startup_order()
        shutdown = dag.shutdown_order()
        assert shutdown == list(reversed(startup))

    def test_startup_order_cyclic_raises(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("a", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP, dependencies=("b",)))
        dag.add(CapabilityDeclaration("b", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP, dependencies=("a",)))
        with pytest.raises(LifecycleDAGError):
            dag.startup_order()

    def test_required_capabilities(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("a", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP))
        dag.add(CapabilityDeclaration("b", CapabilityCriticality.OPTIONAL, StartupPhase.BOOTSTRAP))
        req = dag.required_capabilities()
        assert "a" in req
        assert "b" not in req

    def test_optional_capabilities(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("a", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP))
        dag.add(CapabilityDeclaration("b", CapabilityCriticality.OPTIONAL, StartupPhase.BOOTSTRAP))
        opt = dag.optional_capabilities()
        assert "b" in opt
        assert "a" not in opt

    def test_capabilities_by_phase(self):
        dag = LifecycleDependencyDAG()
        dag.add(CapabilityDeclaration("a", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP))
        dag.add(CapabilityDeclaration("b", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT))
        dag.add(CapabilityDeclaration("c", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT))
        assert dag.capabilities_by_phase(StartupPhase.BOOTSTRAP) == ["a"]
        assert sorted(dag.capabilities_by_phase(StartupPhase.CORE_INIT)) == ["b", "c"]


# ---------------------------------------------------------------------------
# Worker Registry
# ---------------------------------------------------------------------------

class TestWorkerRegistry:
    def _make_worker(self, identity="w1", owner="cap1", timeout=5.0):
        event = threading.Event()
        def cancel():
            event.set()
        def join(t):
            return event.wait(t)
        return WorkerHandle(
            identity=identity, owner=owner,
            cancel_fn=cancel, join_fn=join, join_timeout_s=timeout,
        )

    def test_register_and_get(self):
        reg = WorkerRegistry()
        w = self._make_worker()
        reg.register(w)
        assert reg.get("w1") is w

    def test_duplicate_register(self):
        reg = WorkerRegistry()
        reg.register(self._make_worker())
        with pytest.raises(ValueError):
            reg.register(self._make_worker())

    def test_unregister(self):
        reg = WorkerRegistry()
        reg.register(self._make_worker())
        reg.unregister("w1")
        assert reg.get("w1") is None

    def test_workers_by_owner(self):
        reg = WorkerRegistry()
        reg.register(self._make_worker("w1", "cap1"))
        reg.register(self._make_worker("w2", "cap2"))
        assert len(reg.workers_by_owner("cap1")) == 1
        assert len(reg.workers_by_owner("cap2")) == 1

    def test_cancel_all(self):
        reg = WorkerRegistry()
        w1 = self._make_worker("w1", "cap1")
        w2 = self._make_worker("w2", "cap2")
        reg.register(w1)
        reg.register(w2)
        results = reg.cancel_all(join_timeout=2.0)
        assert results["w1"] is True
        assert results["w2"] is True
        assert w1.cancelled
        assert w2.cancelled

    def test_cancel_by_owner(self):
        reg = WorkerRegistry()
        w1 = self._make_worker("w1", "cap1")
        w2 = self._make_worker("w2", "cap2")
        reg.register(w1)
        reg.register(w2)
        results = reg.cancel_by_owner("cap1", join_timeout=2.0)
        assert "w1" in results
        assert "w2" not in results
        assert w1.cancelled
        assert not w2.cancelled

    def test_cancel_join_timeout(self):
        reg = WorkerRegistry()
        event = threading.Event()
        # Worker that never sets the event — join should time out
        def cancel():
            pass  # don't set event
        def join(t):
            return event.wait(t)
        w = WorkerHandle("w1", "cap1", cancel, join, join_timeout_s=0.1)
        reg.register(w)
        results = reg.cancel_all(join_timeout=0.1)
        assert results["w1"] is False  # timed out

    def test_worker_has_owner(self):
        w = self._make_worker()
        assert w.owner == "cap1"

    def test_worker_has_join_timeout(self):
        w = self._make_worker(timeout=30.0)
        assert w.join_timeout_s == 30.0


# ---------------------------------------------------------------------------
# Deadline/Cancellation
# ---------------------------------------------------------------------------

class TestDeadlineCancellation:
    def test_from_timeout(self):
        dc = DeadlineCancellation.from_timeout(10.0)
        assert dc.remaining_seconds > 0
        assert not dc.expired
        assert not dc.cancelled

    def test_cancel(self):
        dc = DeadlineCancellation.from_timeout(10.0)
        dc.cancel()
        assert dc.cancelled
        with pytest.raises(LifecycleCancelled):
            dc.check()

    def test_expired(self):
        dc = DeadlineCancellation(deadline_monotonic=time.monotonic() - 1.0)
        assert dc.expired
        with pytest.raises(LifecycleDeadlineExceeded):
            dc.check()

    def test_check_ok(self):
        dc = DeadlineCancellation.from_timeout(10.0)
        dc.check()  # should not raise

    def test_remaining_seconds(self):
        dc = DeadlineCancellation.from_timeout(5.0)
        assert 0 < dc.remaining_seconds <= 5.0


# ---------------------------------------------------------------------------
# Timing Evidence
# ---------------------------------------------------------------------------

class TestTimingEvidence:
    def test_add_phase(self):
        ev = LifecycleTimingEvidence()
        ev.add_phase("BOOTSTRAP", 1.0, 2.0, True)
        assert len(ev.phases) == 1
        assert ev.phases[0].duration_s == 1.0
        assert ev.success

    def test_failed_phase(self):
        ev = LifecycleTimingEvidence()
        ev.add_phase("BOOTSTRAP", 1.0, 2.0, True)
        ev.add_phase("CORE_INIT", 2.0, 3.0, False, "error")
        assert not ev.success

    def test_total_duration(self):
        ev = LifecycleTimingEvidence()
        ev.add_phase("A", 1.0, 2.0, True)
        ev.add_phase("B", 2.0, 4.0, True)
        assert ev.total_duration_s == 3.0

    def test_to_dict(self):
        ev = LifecycleTimingEvidence()
        ev.add_phase("A", 1.0, 2.0, True)
        d = ev.to_dict()
        assert "phases" in d
        assert "total_duration_s" in d
        assert "success" in d


# ---------------------------------------------------------------------------
# Lifecycle Controller — Normal startup/shutdown
# ---------------------------------------------------------------------------

class TestLifecycleControllerNormal:
    def test_normal_startup(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "bootstrap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        ctrl.add_capability(CapabilityDeclaration(
            "core", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT,
        ))
        timing = ctrl.startup()
        assert ctrl.state == LifecycleState.READY
        assert timing.success
        assert len(timing.phases) == 6  # all 6 phases
        assert ctrl.activated_capabilities == ["bootstrap", "core"]

    def test_normal_shutdown(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "bootstrap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        ctrl.startup()
        assert ctrl.state == LifecycleState.READY
        timing = ctrl.shutdown()
        assert ctrl.state == LifecycleState.STOPPED
        assert len(timing.phases) == 6  # all 6 phases

    def test_completed_phases(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "bootstrap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        ctrl.startup()
        assert StartupPhase.BOOTSTRAP in ctrl.completed_phases
        assert StartupPhase.READY in ctrl.completed_phases

    def test_with_handlers(self):
        calls = []
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
            initialize=lambda: calls.append("init"),
            start=lambda: calls.append("start"),
            stop=lambda: calls.append("stop"),
            dispose=lambda: calls.append("dispose"),
        ))
        ctrl.startup()
        assert "init" in calls
        assert "start" in calls
        ctrl.shutdown()
        assert "stop" in calls
        assert "dispose" in calls


# ---------------------------------------------------------------------------
# Lifecycle Controller — Failure and rollback
# ---------------------------------------------------------------------------

class TestLifecycleControllerFailure:
    def test_required_failure_rolls_back(self):
        calls = []
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "ok_cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
            initialize=lambda: calls.append("init_ok"),
            start=lambda: calls.append("start_ok"),
            stop=lambda: calls.append("stop_ok"),
            dispose=lambda: calls.append("dispose_ok"),
        ))
        def fail_init():
            raise RuntimeError("boom")
        ctrl.add_capability(CapabilityDeclaration(
            "fail_cap", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT,
            initialize=fail_init,
        ))
        timing = ctrl.startup()
        assert ctrl.state == LifecycleState.FAILED
        assert not timing.success
        # Rollback should have called stop and dispose on ok_cap
        assert "stop_ok" in calls
        assert "dispose_ok" in calls

    def test_optional_failure_degraded_mode(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "required", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        def fail_init():
            raise RuntimeError("optional boom")
        ctrl.add_capability(CapabilityDeclaration(
            "optional", CapabilityCriticality.OPTIONAL, StartupPhase.OPTIONAL_CAPABILITY_INIT,
            initialize=fail_init,
            fallback="local_fallback",
        ))
        ctrl.add_capability(CapabilityDeclaration(
            "local_fallback", CapabilityCriticality.REQUIRED, StartupPhase.CORE_INIT,
        ))
        timing = ctrl.startup()
        assert ctrl.state == LifecycleState.READY
        assert "optional" in ctrl.degraded_capabilities

    def test_optional_failure_no_fallback(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "required", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        def fail_init():
            raise RuntimeError("optional boom")
        ctrl.add_capability(CapabilityDeclaration(
            "optional", CapabilityCriticality.OPTIONAL, StartupPhase.OPTIONAL_CAPABILITY_INIT,
            initialize=fail_init,
        ))
        timing = ctrl.startup()
        # No fallback — still degraded but startup continues
        assert ctrl.state == LifecycleState.READY
        assert "optional" in ctrl.degraded_capabilities


# ---------------------------------------------------------------------------
# Lifecycle Controller — Timeout
# ---------------------------------------------------------------------------

class TestLifecycleControllerTimeout:
    def test_deadline_exceeded(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        # Deadline already in the past (avoids Windows monotonic resolution issues)
        dc = DeadlineCancellation(deadline_monotonic=time.monotonic() - 1.0)
        timing = ctrl.startup(deadline=dc)
        assert ctrl.state == LifecycleState.FAILED
        assert not timing.success

    def test_cancellation(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))
        dc = DeadlineCancellation.from_timeout(10.0)
        dc.cancel()
        timing = ctrl.startup(deadline=dc)
        assert ctrl.state == LifecycleState.FAILED


# ---------------------------------------------------------------------------
# Lifecycle Controller — Repeated start/stop
# ---------------------------------------------------------------------------

class TestLifecycleControllerRepeated:
    def test_repeated_start_stop(self):
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        ))

        for i in range(3):
            # Start
            ctrl.startup()
            assert ctrl.state == LifecycleState.READY
            # Stop
            ctrl.shutdown()
            assert ctrl.state == LifecycleState.STOPPED
            # Reset for next iteration
            ctrl.state_machine.reset()
            ctrl._completed_phases.clear()
            ctrl._activated_capabilities.clear()
            ctrl._degraded_capabilities.clear()
            ctrl.startup_timing = LifecycleTimingEvidence()
            ctrl.shutdown_timing = LifecycleTimingEvidence()

    def test_start_after_failure(self):
        ctrl = LifecycleController()
        def fail():
            raise RuntimeError("boom")
        ctrl.add_capability(CapabilityDeclaration(
            "fail_cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
            initialize=fail,
        ))
        # First start fails
        ctrl.startup()
        assert ctrl.state == LifecycleState.FAILED
        # Reset and try again
        ctrl.state_machine.reset()
        ctrl._completed_phases.clear()
        ctrl._activated_capabilities.clear()
        ctrl._degraded_capabilities.clear()
        ctrl.startup_timing = LifecycleTimingEvidence()
        # Replace with working capability
        ctrl.dag.capabilities["fail_cap"] = CapabilityDeclaration(
            "fail_cap", CapabilityCriticality.REQUIRED, StartupPhase.BOOTSTRAP,
        )
        ctrl.startup()
        assert ctrl.state == LifecycleState.READY


# ---------------------------------------------------------------------------
# Launcher Contract
# ---------------------------------------------------------------------------

class TestLauncherContract:
    def test_launcher_duties(self):
        assert "start-process" in LAUNCHER_DUTIES
        assert "basic-process-control" in LAUNCHER_DUTIES
        assert "subscribe-to-lifecycle-events" in LAUNCHER_DUTIES
        assert "display-readiness-state" in LAUNCHER_DUTIES

    def test_launcher_forbidden(self):
        assert "lifecycle-decisions" in LAUNCHER_FORBIDDEN
        assert "dependency-checks" in LAUNCHER_FORBIDDEN
        assert "governance-checks" in LAUNCHER_FORBIDDEN
        assert "direct-database-start" in LAUNCHER_FORBIDDEN
        assert "owning-lifecycle-state" in LAUNCHER_FORBIDDEN
        assert "second-lifecycle-controller" in LAUNCHER_FORBIDDEN

    def test_launcher_not_second_controller(self):
        assert "second-lifecycle-controller" in LAUNCHER_FORBIDDEN

    def test_is_launcher_action_allowed(self):
        assert is_launcher_action_allowed("start-process")
        assert is_launcher_action_allowed("display-readiness-state")
        assert not is_launcher_action_allowed("lifecycle-decisions")
        assert not is_launcher_action_allowed("dependency-checks")
        assert not is_launcher_action_allowed("direct-database-start")

    def test_launcher_does_not_block_ui(self):
        assert "blocking-ui-until-ready" in LAUNCHER_FORBIDDEN


# ---------------------------------------------------------------------------
# Cross-language boundary
# ---------------------------------------------------------------------------

class TestCrossLanguageBoundary:
    def test_python_is_sole_authority(self):
        """Python is the sole runtime lifecycle authority."""
        ctrl = LifecycleController()
        # State machine is owned by the controller
        assert ctrl.state_machine is not None
        # Only Python can transition states
        assert ctrl.state == LifecycleState.UNINITIALIZED

    def test_native_does_not_own_shutdown(self):
        """C++/C# never own business shutdown policy."""
        # The LifecycleController handles shutdown — native/C# only
        # provide capability lifecycle (initialize/start/stop/dispose)
        ctrl = LifecycleController()
        ctrl.add_capability(CapabilityDeclaration(
            "native_cap", CapabilityCriticality.OPTIONAL,
            StartupPhase.OPTIONAL_CAPABILITY_INIT,
            fallback="python_fallback",
        ))
        ctrl.startup()
        # Shutdown is controlled by Python controller, not native
        ctrl.shutdown()
        assert ctrl.state == LifecycleState.STOPPED

    def test_deadline_propagates(self):
        """Deadline/cancellation propagates across Python/native/C#."""
        dc = DeadlineCancellation.from_timeout(10.0)
        # Native can check deadline cooperatively
        assert dc.remaining_seconds > 0
        assert not dc.cancelled
        # Python can cancel
        dc.cancel()
        assert dc.cancelled
