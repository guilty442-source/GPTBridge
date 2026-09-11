"""Transactional outbox store — durable event log (A195/E169).

Per Governance Codex A195:

  * ``STATE-CHANGE:commit-authoritative-state and append-transactional-
    outbox-event in-one-atomic-boundary`` — ``OutboxStore.append`` accepts an
    optional caller-owned ``sqlite3.Connection`` so the event insert shares
    the producer's commit boundary; without one it commits in its own
    immediate transaction.
  * ``EVENT:{entity-id,entity-type,operation,authoritative-revision,
    previous-revision,changed-field-allowlist,invalidation-keys,state-hash,
    backend-generation,release-id,contract-version,sequence,correlation-id,
    committed-at}``.

The publisher lives in :mod:`tasks.state_outbox`; this module holds the
durable store only (split for A185/E160 source-size compliance).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

OUTBOX_SCHEMA_VERSION = 1
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

OUTBOX_RELATIVE_PATH: tuple[str, ...] = (
    "main-system",
    "runtime",
    "state",
    "state-outbox.sqlite3",
)

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


class OutboxStore:
    """Durable SQLite outbox with per-entity monotonic revisions (A195).

    The store is bound to one database file.  By default it owns the
    central ``state-outbox.sqlite3`` under ``main-system/runtime/state``;
    a producer that keeps its authoritative state in its own SQLite
    database may bind a store to that file (``db_path=...``) and pass its
    open transaction to ``append(connection=...)`` so the state commit and
    the outbox append share one atomic boundary.
    """

    def __init__(
        self,
        project_root: Path | str,
        db_path: Path | str | None = None,
    ) -> None:
        root = Path(project_root).resolve()
        self._db_path = (
            Path(db_path).resolve()
            if db_path is not None
            else root.joinpath(*OUTBOX_RELATIVE_PATH)
        )
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path), timeout=5.0, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            self.ensure_schema(conn)
        finally:
            conn.close()

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        """Create the outbox tables on an arbitrary connection.

        Producers that need a shared atomic boundary call this once on
        their own database so ``outbox_events`` / ``entity_revisions``
        co-locate with the authoritative tables — then pass that
        connection to ``append(connection=...)`` inside their commit.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS entity_revisions (
                       entity_id TEXT PRIMARY KEY,
                       revision INTEGER NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS outbox_events (
                       sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                       entity_id TEXT NOT NULL,
                       entity_type TEXT NOT NULL,
                       operation TEXT NOT NULL,
                       authoritative_revision INTEGER NOT NULL,
                       previous_revision INTEGER NOT NULL,
                       changed_field_allowlist TEXT NOT NULL,
                       invalidation_keys TEXT NOT NULL,
                       state_hash TEXT NOT NULL,
                       backend_generation TEXT NOT NULL,
                       release_id TEXT NOT NULL,
                       contract_version TEXT NOT NULL,
                       correlation_id TEXT NOT NULL,
                       committed_at TEXT NOT NULL,
                       recorded_at TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_outbox_entity
                   ON outbox_events (entity_id, sequence)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS outbox_meta (
                       key TEXT PRIMARY KEY,
                       value TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "INSERT OR IGNORE INTO outbox_meta (key, value) "
                "VALUES ('schema_version', ?)",
                (str(OUTBOX_SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # append (the commit-side half of A195 STATE-CHANGE)
    # ------------------------------------------------------------------

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
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Append a state-change event atomically with the state commit.

        When ``connection`` is a caller-owned connection already inside a
        transaction, the revision bump and event insert join that
        transaction — commit-authoritative-state and outbox-append share
        one atomic boundary.  The connection's database must already have
        the outbox schema (``OutboxStore.ensure_schema``); events then live
        in that database, and replay requires a store bound to the same
        file (``db_path=``).  Otherwise this method opens its own
        ``BEGIN IMMEDIATE`` transaction on this store's database.
        """
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
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    revision, sequence = self._insert_row(conn, entity_id, row)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

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

    @staticmethod
    def _insert_row(
        conn: sqlite3.Connection, entity_id: str, row: tuple[Any, ...]
    ) -> tuple[int, int]:
        cur = conn.execute(
            """INSERT INTO entity_revisions (entity_id, revision)
               VALUES (?, 1)
               ON CONFLICT (entity_id) DO UPDATE SET revision = revision + 1
               RETURNING revision""",
            (entity_id,),
        )
        revision = int(cur.fetchone()[0])
        cur = conn.execute(
            """INSERT INTO outbox_events (
                   entity_id, entity_type, operation, authoritative_revision,
                   previous_revision, changed_field_allowlist, invalidation_keys,
                   state_hash, backend_generation, release_id, contract_version,
                   correlation_id, committed_at, recorded_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row[0], row[1], row[2], revision, revision - 1, *row[3:]),
        )
        return revision, int(cur.lastrowid)

    # ------------------------------------------------------------------
    # replay / retention
    # ------------------------------------------------------------------

    def fetch_after(self, sequence: int, limit: int = DRAIN_BATCH_LIMIT) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT sequence, entity_id, entity_type, operation,
                          authoritative_revision, previous_revision,
                          changed_field_allowlist, invalidation_keys,
                          state_hash, backend_generation, release_id,
                          contract_version, correlation_id, committed_at
                   FROM outbox_events
                   WHERE sequence > ?
                   ORDER BY sequence ASC
                   LIMIT ?""",
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
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM outbox_events"
            ).fetchone()
        return int(row[0] or 0)

    def current_revision(self, entity_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT revision FROM entity_revisions WHERE entity_id = ?",
                (str(entity_id),),
            ).fetchone()
        return int(row[0]) if row else 0

    def prune(self, *, below_sequence: int, keep_min: int = RETENTION_MIN_EVENTS) -> int:
        """Delete events older than ``below_sequence``, keeping ``keep_min``.

        Never deletes events at or above ``below_sequence`` — callers pass
        the minimum acked cursor across live sessions so unacked history is
        always replayable.
        """
        floor = max(0, self.max_sequence() - max(0, int(keep_min)))
        cutoff = min(int(below_sequence), floor)
        if cutoff <= 0:
            return 0
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM outbox_events WHERE sequence < ?", (cutoff,)
            )
            return int(cur.rowcount or 0)


__all__ = [
    "DELIVERY_WINDOW",
    "DRAIN_BATCH_LIMIT",
    "OUTBOX_CONTRACT_VERSION",
    "OUTBOX_EVENT_NAME",
    "OUTBOX_RELATIVE_PATH",
    "POLL_INTERVAL_SECONDS",
    "RETENTION_MIN_EVENTS",
    "RETRY_INTERVAL_SECONDS",
    "OutboxStore",
    "_EVENT_REQUIRED_FIELDS",
    "_iso_now",
]
