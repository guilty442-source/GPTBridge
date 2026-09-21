"""G52 event sequence and recovery evidence."""
from __future__ import annotations

from pathlib import Path

from tasks.state_outbox import OutboxPublisher
from tasks.state_outbox_store import OutboxStore


class _Ui:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    def send(self, command: str, payload: object) -> None:
        self.messages.append((command, payload))


def test_g52_outbox_sequence_and_entity_revision_are_monotonic(tmp_path: Path) -> None:
    store = OutboxStore(tmp_path / "outbox.sqlite3")

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


def test_g52_reconnect_generation_resets_cursor_and_ack_never_moves_back(tmp_path: Path) -> None:
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

    current = publisher.handle_hello(ui, cursor=3, generation=publisher.backend_generation)
    assert current["reset"] is False
    assert current["cursor"] == 3
