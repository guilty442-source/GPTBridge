"""Transactional outbox store — durable event log (A195/E169).

Per Governance Codex A195:

  * ``STATE-CHANGE:commit-authoritative-state and append-transactional-
    outbox-event in-one-atomic-boundary`` — ``PgOutboxStore.append`` accepts
    an optional caller-owned PostgreSQL connection so the event insert
    shares the producer's commit boundary; without one it commits in its
    own immediate transaction.
  * ``EVENT:{entity-id,entity-type,operation,authoritative-revision,
    previous-revision,changed-field-allowlist,invalidation-keys,state-hash,
    backend-generation,release-id,contract-version,sequence,correlation-id,
    committed-at}``.

The publisher lives in :mod:`tasks.state_outbox`; this module holds the
durable store only (split for A430/E160 source-size compliance).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

OUTBOX_EVENT_NAME = "state_event"
OUTBOX_CONTRACT_VERSION = "a195.state-event.v1"

# Bounded delivery window: at most this many events may be in-flight
# (sent but not yet client-acknowledged) per session.
DELIVERY_WINDOW = 200
# How long an unacked batch may sit before it is re-sent (at-least-once).
RETRY_INTERVAL_SECONDS = 2.0
# Publisher wake interval when no append signal arrives.
POLL_INTERVAL_SECONDS = 0.5
# Retention: never drop events that could still be replayed; keep at least
# this many recent events even when every session has acked past them.
RETENTION_MIN_EVENTS = 5000
# Per-drain send cap per session.
DRAIN_BATCH_LIMIT = 100

_EVENT_REQUIRED_FIELDS = (
    "entity_id",
    "entity_type",
    "operation",
    "authoritative_revision",
    "previous_revision",
    "changed_field_allowlist",
    "invalidation_keys",
    "state_hash",
    "backend_generation",
    "release_id",
    "contract_version",
    "sequence",
    "correlation_id",
    "committed_at",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()



class PgOutboxStore:
    """Authoritative A195 outbox in the PostgreSQL transport schema.

    Per-entity monotonic revision and event insert share one transaction
    against ``<schema>.outbox_event`` / ``outbox_entity_revision``
    (migration 147), and ``fetch_after`` replays by monotonic sequence.
    PostgreSQL is the sole structured-data authority (A610/A621); the
    ``schema`` parameter exists for isolated test schemas — production
    always uses the default ``gptbridge_transport``.
    """

    def __init__(
        self,
        project_root: Path | str,
        db_path: Path | str | None = None,
        schema: str = "gptbridge_transport",
    ) -> None:
        del project_root, db_path  # authority lives in PostgreSQL, not a file
        import psycopg
        from psycopg import sql as _sql

        from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn

        self._psycopg = psycopg
        self._sql = _sql
        self._lock = threading.Lock()
        self._events = _sql.SQL("{}.outbox_event").format(
            _sql.Identifier(schema)
        )
        self._revisions = _sql.SQL("{}.outbox_entity_revision").format(
            _sql.Identifier(schema)
        )
        # Perf/fail-fast: bound the connect so a down DB fails in seconds
        # instead of hanging the outbox/publisher startup indefinitely.
        self._conn = psycopg.connect(
            resolve_dsn(DsnPurpose.RUNTIME).dsn, connect_timeout=5
        )

    def append(
        self,
        *,
        entity_id: str,
        entity_type: str,
        operation: str,
        changed_field_allowlist: Iterable[str] = (),
        invalidation_keys: Iterable[str] = (),
        state_hash: str = "",
        backend_generation: str = "",
        release_id: str = "",
        contract_version: str = OUTBOX_CONTRACT_VERSION,
        correlation_id: str = "",
        connection: Any = None,
    ) -> dict[str, Any]:
        entity_id = str(entity_id or "").strip()
        entity_type = str(entity_type or "").strip()
        operation = str(operation or "").strip()
        if not entity_id or not entity_type or not operation:
            raise ValueError("outbox event requires entity_id/entity_type/operation")

        committed_at = _iso_now()
        row = (
            entity_id,
            entity_type,
            operation,
            json.dumps(list(changed_field_allowlist), ensure_ascii=False),
            json.dumps(list(invalidation_keys), ensure_ascii=False),
            str(state_hash or ""),
            str(backend_generation or ""),
            str(release_id or ""),
            str(contract_version or OUTBOX_CONTRACT_VERSION),
            str(correlation_id or ""),
            committed_at,
            committed_at,
        )

        if connection is not None:
            revision, sequence = self._insert_row(connection, entity_id, row)
        else:
            with self._lock, self._conn.transaction():
                revision, sequence = self._insert_row(self._conn, entity_id, row)

        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "operation": operation,
            "authoritative_revision": revision,
            "previous_revision": revision - 1,
            "changed_field_allowlist": list(changed_field_allowlist),
            "invalidation_keys": list(invalidation_keys),
            "state_hash": str(state_hash or ""),
            "backend_generation": str(backend_generation or ""),
            "release_id": str(release_id or ""),
            "contract_version": str(contract_version or OUTBOX_CONTRACT_VERSION),
            "sequence": sequence,
            "correlation_id": str(correlation_id or ""),
            "committed_at": committed_at,
        }

    def _insert_row(self, conn: Any, entity_id: str, row: tuple[Any, ...]) -> tuple[int, int]:
        cur = conn.execute(
            self._sql.SQL(
                "INSERT INTO {} (entity_id, revision) VALUES (%s, 1) "
                "ON CONFLICT (entity_id) DO UPDATE SET revision = "
                "outbox_entity_revision.revision + 1 RETURNING revision"
            ).format(self._revisions),
            (entity_id,),
        )
        revision = int(cur.fetchone()[0])
        cur = conn.execute(
            self._sql.SQL(
                "INSERT INTO {} (entity_id, entity_type, operation, "
                "authoritative_revision, previous_revision, "
                "changed_field_allowlist, invalidation_keys, state_hash, "
                "backend_generation, release_id, contract_version, "
                "correlation_id, committed_at, recorded_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING sequence"
            ).format(self._events),
            (row[0], row[1], row[2], revision, revision - 1, *row[3:]),
        )
        return revision, int(cur.fetchone()[0])

    def fetch_after(self, sequence: int, limit: int = DRAIN_BATCH_LIMIT) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                self._sql.SQL(
                    "SELECT sequence, entity_id, entity_type, operation, "
                    "authoritative_revision, previous_revision, "
                    "changed_field_allowlist, invalidation_keys, "
                    "state_hash, backend_generation, release_id, "
                    "contract_version, correlation_id, committed_at "
                    "FROM {} WHERE sequence > %s "
                    "ORDER BY sequence ASC LIMIT %s"
                ).format(self._events),
                (int(sequence), max(1, int(limit))),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for r in rows:
            events.append(
                {
                    "sequence": int(r[0]),
                    "entity_id": str(r[1]),
                    "entity_type": str(r[2]),
                    "operation": str(r[3]),
                    "authoritative_revision": int(r[4]),
                    "previous_revision": int(r[5]),
                    "changed_field_allowlist": json.loads(r[6] or "[]"),
                    "invalidation_keys": json.loads(r[7] or "[]"),
                    "state_hash": str(r[8]),
                    "backend_generation": str(r[9]),
                    "release_id": str(r[10]),
                    "contract_version": str(r[11]),
                    "correlation_id": str(r[12]),
                    "committed_at": str(r[13]),
                }
            )
        return events

    def max_sequence(self) -> int:
        with self._lock:
            row = self._conn.execute(
                self._sql.SQL(
                    "SELECT COALESCE(MAX(sequence), 0) FROM {}"
                ).format(self._events)
            ).fetchone()
        return int(row[0] or 0)

    def current_revision(self, entity_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                self._sql.SQL(
                    "SELECT revision FROM {} WHERE entity_id = %s"
                ).format(self._revisions),
                (str(entity_id),),
            ).fetchone()
        return int(row[0]) if row else 0

    def prune(self, *, below_sequence: int, keep_min: int = RETENTION_MIN_EVENTS) -> int:
        floor = max(0, self.max_sequence() - max(0, int(keep_min)))
        cutoff = min(int(below_sequence), floor)
        if cutoff <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                self._sql.SQL(
                    "DELETE FROM {} WHERE sequence < %s"
                ).format(self._events),
                (cutoff,),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def build_outbox_store(project_root: Path | str) -> PgOutboxStore:
    """Build the outbox store (A501 bounded fallback contract).

    PostgreSQL is the sole authority (A610/A621); the SQLite fallback was
    retired with the migration window.  ``GPTBRIDGE_OUTBOX_ENGINE`` must
    be ``postgresql`` (the default) — any other value fails closed.
    """

    engine = str(os.environ.get("GPTBRIDGE_OUTBOX_ENGINE", "") or "postgresql")
    engine = engine.strip().lower()
    if engine == "postgresql":
        return PgOutboxStore(project_root)
    raise RuntimeError(f"OUTBOX_ENGINE_UNSUPPORTED:{engine}")


__all__ = [
    "DELIVERY_WINDOW",
    "DRAIN_BATCH_LIMIT",
    "OUTBOX_CONTRACT_VERSION",
    "OUTBOX_EVENT_NAME",
    "POLL_INTERVAL_SECONDS",
    "RETENTION_MIN_EVENTS",
    "RETRY_INTERVAL_SECONDS",
    "PgOutboxStore",
    "build_outbox_store",
    "_EVENT_REQUIRED_FIELDS",
    "_iso_now",
]
