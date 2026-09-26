"""A374 runtime state machine lifecycle tests.

Machine states (A374, exactly four): STARTING / CANONICAL / DEGRADED /
RECONCILING.  The status surface additionally exposes the derived
RECONCILIATION_FAILED outcome when a reconciliation attempt fails while
the service stays bounded at DEGRADED.
"""

from __future__ import annotations

import uuid

import psycopg

import pytest

from core_system.rag.runtime_state import (
    RagRuntimeState,
    RagRuntimeStateMachine,
    ReconciliationQueue,
    TransitionError,
)


@pytest.fixture
def queue_conn():
    schema = "rrq_test_" + uuid.uuid4().hex[:12]
    from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn
    dsn = resolve_dsn(DsnPurpose.ADMIN).dsn
    with psycopg.connect(dsn, connect_timeout=5) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'GRANT USAGE, CREATE ON SCHEMA "{schema}" TO gptbridge_runtime')
        c.commit()
    from shared_layer.local.pg_adapter import connect as pg_connect
    conn = pg_connect(schema)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            with psycopg.connect(dsn, connect_timeout=5) as c:
                c.execute(f'DROP SCHEMA "{schema}" CASCADE')
                c.commit()
        except Exception:
            pass


def _machine(queue_conn) -> RagRuntimeStateMachine:
    return RagRuntimeStateMachine(ReconciliationQueue(queue_conn))


def test_startup_to_canonical(queue_conn) -> None:
    m = _machine(queue_conn)
    state = m.evaluate_startup(
        vector_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    assert state == RagRuntimeState.CANONICAL
    assert m.effective_state == "CANONICAL"
    assert m.reconciliation_required is False


def test_startup_to_degraded_when_store_down(queue_conn) -> None:
    m = _machine(queue_conn)
    state = m.evaluate_startup(
        vector_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    assert state == RagRuntimeState.DEGRADED
    assert m.reconciliation_required is True


def test_canonical_failure_degrades(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    state = m.report_canonical_failure("vector unreachable")
    assert state == RagRuntimeState.DEGRADED
    assert m.effective_state == "DEGRADED"
    assert m.reconciliation_failed is False


def test_recovery_cycle_degraded_reconciling_canonical(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    m.report_canonical_failure("vector blip")
    state = m.begin_reconciliation(vector_healthy=True, postgresql_healthy=True)
    assert state == RagRuntimeState.RECONCILING
    assert m.effective_state == "RECONCILING"
    state = m.complete_reconciliation(
        counts_match=True, ids_match=True, hashes_match=True, versions_match=True
    )
    assert state == RagRuntimeState.CANONICAL
    assert m.reconciliation_required is False


def test_reconciliation_failure_surfaces_derived_state(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    m.report_canonical_failure("pg blip")
    m.begin_reconciliation(vector_healthy=True, postgresql_healthy=True)
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


def test_reconciliation_parity_failure_is_reported(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    m.begin_reconciliation(vector_healthy=True, postgresql_healthy=True)
    state = m.complete_reconciliation(
        counts_match=False, ids_match=True, hashes_match=True, versions_match=True
    )
    assert state == RagRuntimeState.DEGRADED
    assert m.effective_state == "RECONCILIATION_FAILED"


def test_retry_clears_failed_flag_and_recovers(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    m.begin_reconciliation(vector_healthy=True, postgresql_healthy=True)
    m.fail_reconciliation("transient")
    assert m.effective_state == "RECONCILIATION_FAILED"
    m.begin_reconciliation(vector_healthy=True, postgresql_healthy=True)
    assert m.reconciliation_failed is False
    m.complete_reconciliation(
        counts_match=True, ids_match=True, hashes_match=True, versions_match=True
    )
    assert m.effective_state == "CANONICAL"


def test_begin_reconciliation_requires_healthy_stores(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=False, postgresql_healthy=False, index_state_matches=False
    )
    with pytest.raises(Exception):
        m.begin_reconciliation(vector_healthy=False, postgresql_healthy=True)


def test_forbidden_transitions_raise(queue_conn) -> None:
    m = _machine(queue_conn)
    with pytest.raises(TransitionError):
        m.begin_reconciliation(vector_healthy=True, postgresql_healthy=True)
    m.evaluate_startup(
        vector_healthy=True, postgresql_healthy=True, index_state_matches=True
    )
    with pytest.raises(TransitionError):
        m.evaluate_startup(
            vector_healthy=True, postgresql_healthy=True, index_state_matches=True
        )


def test_report_failure_idempotent_while_degraded(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=False, postgresql_healthy=True, index_state_matches=True
    )
    state = m.report_canonical_failure("still down")
    assert state == RagRuntimeState.DEGRADED
    assert m.status()["last_error"] == "still down"


def test_seconds_in_state_counts_from_last_transition(queue_conn) -> None:
    m = _machine(queue_conn)
    m.evaluate_startup(
        vector_healthy=False, postgresql_healthy=False, index_state_matches=False
    )
    assert m.state == RagRuntimeState.DEGRADED
    secs = m.seconds_in_state
    assert secs >= 0.0


def test_seconds_in_state_invalid_timestamp_is_zero(queue_conn) -> None:
    m = _machine(queue_conn)
    m._last_transition_at = "not-a-timestamp"
    assert m.seconds_in_state == 0.0
