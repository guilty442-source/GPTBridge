"""A374 runtime state machine lifecycle tests.

Machine states (A374, exactly four): STARTING / CANONICAL / DEGRADED /
RECONCILING.  The status surface additionally exposes the derived
RECONCILIATION_FAILED outcome when a reconciliation attempt fails while
the service stays bounded at DEGRADED.
"""

from __future__ import annotations

import sqlite3

import pytest

from core_system.rag.runtime_state import (
    RagRuntimeState,
    RagRuntimeStateMachine,
    ReconciliationQueue,
    TransitionError,
)


def _machine() -> RagRuntimeStateMachine:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    return RagRuntimeStateMachine(ReconciliationQueue(conn))


def test_startup_to_canonical() -> None:
    m = _machine()
    state = m.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    assert state == RagRuntimeState.CANONICAL
    assert m.effective_state == "CANONICAL"
    assert m.reconciliation_required is False


def test_startup_to_degraded_when_store_down() -> None:
    m = _machine()
    state = m.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    assert state == RagRuntimeState.DEGRADED
    assert m.reconciliation_required is True


def test_canonical_failure_degrades() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    state = m.report_canonical_failure("qdrant unreachable")
    assert state == RagRuntimeState.DEGRADED
    assert m.effective_state == "DEGRADED"
    assert m.reconciliation_failed is False


def test_recovery_cycle_degraded_reconciling_canonical() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    m.report_canonical_failure("qdrant blip")
    state = m.begin_reconciliation(qdrant_healthy=True, postgresql_healthy=True)
    assert state == RagRuntimeState.RECONCILING
    assert m.effective_state == "RECONCILING"
    state = m.complete_reconciliation(
        counts_match=True, ids_match=True, hashes_match=True, versions_match=True
    )
    assert state == RagRuntimeState.CANONICAL
    assert m.reconciliation_required is False


def test_reconciliation_failure_surfaces_derived_state() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    m.report_canonical_failure("pg blip")
    m.begin_reconciliation(qdrant_healthy=True, postgresql_healthy=True)
    state = m.fail_reconciliation("hash mismatch during verify")
    # Service stays bounded at DEGRADED; the status surface reports the
    # derived RECONCILIATION_FAILED outcome.
    assert state == RagRuntimeState.DEGRADED
    assert m.effective_state == "RECONCILIATION_FAILED"
    assert m.reconciliation_failed is True
    assert m.reconciliation_required is True
    status = m.status()
    assert status["effective_state"] == "RECONCILIATION_FAILED"
    assert status["last_error"] == "hash mismatch during verify"


def test_reconciliation_parity_failure_is_reported() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    m.begin_reconciliation(qdrant_healthy=True, postgresql_healthy=True)
    state = m.complete_reconciliation(
        counts_match=False, ids_match=True, hashes_match=True, versions_match=True
    )
    assert state == RagRuntimeState.DEGRADED
    assert m.effective_state == "RECONCILIATION_FAILED"


def test_retry_clears_failed_flag_and_recovers() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    m.begin_reconciliation(qdrant_healthy=True, postgresql_healthy=True)
    m.fail_reconciliation("transient")
    assert m.effective_state == "RECONCILIATION_FAILED"
    m.begin_reconciliation(qdrant_healthy=True, postgresql_healthy=True)
    assert m.reconciliation_failed is False
    m.complete_reconciliation(
        counts_match=True, ids_match=True, hashes_match=True, versions_match=True
    )
    assert m.effective_state == "CANONICAL"


def test_begin_reconciliation_requires_healthy_stores() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=False, index_state_matches=False
    )
    with pytest.raises(Exception):
        m.begin_reconciliation(qdrant_healthy=False, postgresql_healthy=True)


def test_forbidden_transitions_raise() -> None:
    m = _machine()
    with pytest.raises(TransitionError):
        m.begin_reconciliation(qdrant_healthy=True, postgresql_healthy=True)
    m.evaluate_startup(
        qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    with pytest.raises(TransitionError):
        m.evaluate_startup(
            qdrant_healthy=True, postgresql_healthy=True, index_state_matches=True
        )


def test_report_failure_idempotent_while_degraded() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    state = m.report_canonical_failure("still down")
    assert state == RagRuntimeState.DEGRADED
    assert m.status()["last_error"] == "still down"


def test_seconds_in_state_counts_from_last_transition() -> None:
    m = _machine()
    m.evaluate_startup(
        qdrant_healthy=False, postgresql_healthy=False, index_state_matches=False
    )
    assert m.state == RagRuntimeState.DEGRADED
    secs = m.seconds_in_state
    assert secs >= 0.0


def test_seconds_in_state_invalid_timestamp_is_zero() -> None:
    m = _machine()
    m._last_transition_at = "not-a-timestamp"
    assert m.seconds_in_state == 0.0
