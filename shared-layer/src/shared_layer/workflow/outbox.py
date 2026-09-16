"""Transactional outbox and inbox de-duplication.

Outbox: the PG transaction writes business rows + operation state +
outbox_event together; a worker drains the outbox afterwards, so a crash
between "PG committed" and "Qdrant called" cannot lose the follow-up step.

Inbox: inbound cross-module messages are recorded with their
idempotency key; a redelivery returns the stored result instead of
re-executing the operation.

Central shared transport stays PostgreSQL; SQLite may only carry a
module-private ``local_outbox`` for degraded fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

OUTBOX_TABLE: Final[str] = "gptbridge_workflow.outbox_event"
INBOX_TABLE: Final[str] = "gptbridge_workflow.inbox_message"

# Delivered at least once; execution must be idempotent (never exactly-once).
DELIVERY_CONTRACT: Final[str] = "at-least-once + idempotent execution + deterministic dedup"

OUTBOX_ENQUEUE_SQL: Final[str] = (
    "INSERT INTO gptbridge_workflow.outbox_event "
    "(event_id, operation_id, step_id, engine, payload, status, created_at) "
    "VALUES (%s, %s, %s, %s, %s, 'PENDING', now())"
)

OUTBOX_FETCH_SQL: Final[str] = (
    "SELECT event_id, operation_id, step_id, engine, payload "
    "FROM gptbridge_workflow.outbox_event "
    "WHERE status = 'PENDING' ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED"
)

INBOX_INSERT_SQL: Final[str] = (
    "INSERT INTO gptbridge_workflow.inbox_message "
    "(message_id, idempotency_key, source_module, payload_hash, result, status, processed_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, now()) ON CONFLICT (idempotency_key) DO NOTHING"
)

INBOX_LOOKUP_SQL: Final[str] = (
    "SELECT message_id, result, status FROM gptbridge_workflow.inbox_message "
    "WHERE idempotency_key = %s"
)


@dataclass
class InboxEntry:
    message_id: str
    idempotency_key: str
    source_module: str
    payload_hash: str = ""
    result: dict[str, Any] | None = None
    status: str = "processed"


@dataclass
class Inbox:
    """In-memory mirror of the inbox table for tests and local dedup."""

    entries: dict[str, InboxEntry] = field(default_factory=dict)

    def seen(self, idempotency_key: str) -> InboxEntry | None:
        return self.entries.get(idempotency_key)

    def record(self, entry: InboxEntry) -> InboxEntry:
        if not entry.idempotency_key:
            raise ValueError("INBOX_IDEMPOTENCY_KEY_REQUIRED")
        existing = self.entries.get(entry.idempotency_key)
        if existing is not None:
            return existing
        self.entries[entry.idempotency_key] = entry
        return entry


@dataclass
class OutboxEvent:
    event_id: str
    operation_id: str
    step_id: str
    engine: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "PENDING"

    def parameters(self) -> tuple[Any, ...]:
        return (self.event_id, self.operation_id, self.step_id, self.engine, self.payload)


def deduplicated_result(
    inbox: Inbox,
    idempotency_key: str,
    fallback: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return (result, was_duplicate) without re-executing the operation."""
    existing = inbox.seen(idempotency_key)
    if existing is not None:
        return (existing.result if existing.result is not None else {}, True)
    return fallback, False


__all__ = [
    "DELIVERY_CONTRACT",
    "INBOX_INSERT_SQL",
    "INBOX_LOOKUP_SQL",
    "INBOX_TABLE",
    "OUTBOX_ENQUEUE_SQL",
    "OUTBOX_FETCH_SQL",
    "OUTBOX_TABLE",
    "Inbox",
    "InboxEntry",
    "OutboxEvent",
    "deduplicated_result",
]
