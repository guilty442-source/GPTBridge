"""G52 event sequence and recovery evidence."""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from shared_layer.security.dsn_policy import DsnPurpose, resolve_dsn
from tasks.state_outbox import OutboxPublisher
from tasks.state_outbox_store import PgOutboxStore


def _create_schema(schema: str) -> None:
    with psycopg.connect(resolve_dsn(DsnPurpose.ADMIN).dsn, connect_timeout=5) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(
            f'CREATE TABLE "{schema}".outbox_entity_revision ('
            "entity_id text PRIMARY KEY, revision bigint NOT NULL)"
        )
        c.execute(
            f'CREATE TABLE "{schema}".outbox_event ('
            "sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "entity_id text NOT NULL, entity_type text NOT NULL, "
            "operation text NOT NULL, authoritative_revision bigint NOT NULL, "
            "previous_revision bigint NOT NULL, "
            "changed_field_allowlist text NOT NULL, "
            "invalidation_keys text NOT NULL, state_hash text NOT NULL, "
            "backend_generation text NOT NULL, release_id text NOT NULL, "
            "contract_version text NOT NULL, correlation_id text NOT NULL, "
            "committed_at text NOT NULL, recorded_at text NOT NULL)"
        )
        c.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO gptbridge_runtime')
        c.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
            f'"{schema}" TO gptbridge_runtime'
        )
        c.commit()


@pytest.fixture()
def pg_outbox(tmp_path: Path):
    """Isolated PostgreSQL schema per test (A610/A621: no SQLite)."""
    schema = f"outbox_test_{uuid.uuid4().hex[:12]}"
    _create_schema(schema)
    store = PgOutboxStore(tmp_path, schema=schema)
    try:
        yield store
    finally:
        try:
            store.close()
        except Exception:
            pass
        try:
            with psycopg.connect(
                resolve_dsn(DsnPurpose.ADMIN).dsn, connect_timeout=5
            ) as c:
                c.execute(f'DROP SCHEMA "{schema}" CASCADE')
                c.commit()
        except Exception:
            pass


class _Ui:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    def send(self, command: str, payload: object) -> None:
        self.messages.append((command, payload))


def test_g52_outbox_sequence_and_entity_revision_are_monotonic(
    pg_outbox: PgOutboxStore,
) -> None:
    store = pg_outbox

    first = store.append(
        entity_id="runtime",
        entity_type="runtime-status",
        operation="started",
        backend_generation="gen-1",
        release_id="rel-1",
    )
    second = store.append(
        entity_id="runtime",
        entity_type="runtime-status",
        operation="ready",
        backend_generation="gen-1",
        release_id="rel-1",
    )

    assert second["sequence"] > first["sequence"]
    assert first["authoritative_revision"] == 1
    assert second["previous_revision"] == 1
    assert store.fetch_after(first["sequence"]) == [second]


def test_g52_reconnect_generation_resets_cursor_and_ack_never_moves_back(
    tmp_path: Path,
    pg_outbox: PgOutboxStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tasks.state_outbox.build_outbox_store", lambda root: pg_outbox
    )
    publisher = OutboxPublisher(type("App", (), {"project_root": tmp_path})())
    ui = _Ui()
    publisher.register_session(ui)

    first = publisher.handle_hello(ui, cursor=7, generation="old-generation")
    assert first["reset"] is True
    assert first["cursor"] == 0

    publisher.handle_ack(ui, 3)
    publisher.handle_ack(ui, 1)
    session = publisher._session_for(ui)
    assert session is not None
    assert session["acked"] == 3

    current = publisher.handle_hello(
        ui, cursor=3, generation=publisher.backend_generation
    )
    assert current["reset"] is False
    assert current["cursor"] == 3
