"""Durable operation authority on the migration-113 SQL contract.

``gptbridge_workflow.operation`` / ``operation_step`` / ``operation_event``
are the single authority for multi-engine work.  This module provides the
store contract plus two implementations:

  * ``PostgresSagaStore`` — parameterized SQL against migration
    ``113_workflow_operation.sql`` (lease ``claimed_by`` / ``lease_until``,
    idempotency key, checkpoints, events).  No new tables and no new columns.
  * ``InMemorySagaStore`` — an offline mirror with the same claim/lease
    semantics, used before PostgreSQL is reachable and in tests.  It is
    explicitly non-canonical: it can never declare central completion on its
    own, exactly like the SQLite fallback.

Nothing here decides business outcomes; the store only persists and claims
what the saga / reconcile workers decide.
"""

from __future__ import annotations


from .persistence_contract import SagaStore, SagaStoreError, validate_event_type
from .persistence_memory import InMemorySagaStore, StoredEvent
from .persistence_postgres import PostgresSagaStore
from .persistence_sql import (
    OPERATION_CLAIM_NEXT_SQL,
    OPERATION_CLAIM_SQL,
    OPERATION_EVENT_INSERT_SQL,
    OPERATION_HEARTBEAT_SQL,
    OPERATION_INSERT_SQL,
    OPERATION_LIST_SQL,
    OPERATION_LOAD_BY_FINGERPRINT_SQL,
    OPERATION_LOAD_BY_KEY_SQL,
    OPERATION_LOAD_SQL,
    OPERATION_SAVE_SQL,
    OPERATION_STEP_LIST_SQL,
    OPERATION_STEP_UPSERT_SQL,
    RECONCILE_CLAIM_SQL,
)


__all__ = [
    "InMemorySagaStore",
    "OPERATION_CLAIM_NEXT_SQL",
    "OPERATION_CLAIM_SQL",
    "OPERATION_EVENT_INSERT_SQL",
    "OPERATION_HEARTBEAT_SQL",
    "OPERATION_INSERT_SQL",
    "OPERATION_LOAD_BY_FINGERPRINT_SQL",
    "OPERATION_LOAD_BY_KEY_SQL",
    "OPERATION_LIST_SQL",
    "OPERATION_LOAD_SQL",
    "OPERATION_SAVE_SQL",
    "OPERATION_STEP_LIST_SQL",
    "OPERATION_STEP_UPSERT_SQL",
    "PostgresSagaStore",
    "RECONCILE_CLAIM_SQL",
    "SagaStore",
    "SagaStoreError",
    "StoredEvent",
    "validate_event_type",
]
