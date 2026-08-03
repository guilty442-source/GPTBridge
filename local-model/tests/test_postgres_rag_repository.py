from __future__ import annotations

from contextlib import contextmanager

from backend.services.local_ai.infrastructure.postgres_rag_repository import (
    PostgresRagRepository,
)


class _Cursor:
    def fetchone(self):
        return {"document_count": 2, "chunk_count": 7, "character_count": 1200}


class _Connection:
    def __init__(self) -> None:
        self.query = ""

    def execute(self, query: str):
        self.query = query
        return _Cursor()


def test_status_uses_index_metadata_without_physical_chunk_content() -> None:
    connection = _Connection()
    repository = object.__new__(PostgresRagRepository)

    @contextmanager
    def connect():
        yield connection

    repository._connect = connect  # type: ignore[method-assign]

    status = repository.status()

    assert "length(content)" not in connection.query.casefold()
    assert "gptbridge_rag.index_state" in connection.query
    assert status == {
        "engine": "postgresql",
        "schema": "gptbridge_rag",
        "fts_enabled": True,
        "content_storage": "excluded-by-architecture",
        "document_count": 2,
        "chunk_count": 7,
        "character_count": 1200,
    }
