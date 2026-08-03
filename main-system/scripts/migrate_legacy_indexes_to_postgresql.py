from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import psycopg


ROOT = Path(__file__).resolve().parents[2]
CENTRAL_SCHEMA = ROOT / "shared-layer" / "sql" / "central_index.sql"
CHANNELS = {
    "system": ROOT / "shared-layer" / "data" / "system-channel.sqlite3",
    "ai": ROOT / "shared-layer" / "data" / "ai-channel.sqlite3",
}
RAG_DATABASE = ROOT / "local-model" / "runtime" / "state" / "local-rag-keywords.sqlite3"


def _dsn() -> str:
    value = str(os.environ.get("GPTBRIDGE_POSTGRES_DSN") or "").strip()
    if not value:
        raise RuntimeError("GPTBRIDGE_POSTGRES_DSN_REQUIRED")
    return value


def _remove_sqlite_family(database: Path) -> bool:
    removed = True
    for candidate in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if candidate.exists():
            try:
                candidate.unlink()
            except PermissionError:
                # Active legacy readers may briefly retain a Windows handle.
                # Migration is already committed; cleanup retries on next repair.
                removed = False
    return removed


def _migrate_channel(connection: psycopg.Connection[Any], channel: str, database: Path) -> int:
    if not database.is_file():
        return 0
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        rows = source.execute("SELECT * FROM tool_requests").fetchall()
    for row in rows:
        connection.execute(
            """
            INSERT INTO gptbridge_transport.tool_request (
                channel_id, request_id, requester_actor, target_tool_id,
                payload, status, response, progress, created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s))
            ON CONFLICT (channel_id, request_id) DO UPDATE SET
                status=EXCLUDED.status, response=EXCLUDED.response,
                progress=EXCLUDED.progress, updated_at=EXCLUDED.updated_at
            """,
            (
                channel, row["request_id"], row["requester_actor"],
                row["target_tool_id"], row["payload_json"], row["status"],
                row["response_json"], row["progress_json"],
                row["created_at"], row["updated_at"],
            ),
        )
    migrated = connection.execute(
        "SELECT count(*) FROM gptbridge_transport.tool_request WHERE channel_id=%s",
        (channel,),
    ).fetchone()[0]
    if int(migrated) < len(rows):
        raise RuntimeError(f"CHANNEL_MIGRATION_COUNT_MISMATCH:{channel}")
    return len(rows)


def _migrate_rag(connection: psycopg.Connection[Any]) -> int:
    if not RAG_DATABASE.is_file():
        return 0
    with sqlite3.connect(f"{RAG_DATABASE.as_uri()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        documents = source.execute("SELECT * FROM rag_document").fetchall()
        chunks = source.execute("SELECT * FROM rag_chunk ORDER BY document_id, sequence").fetchall()
    by_document: dict[str, list[sqlite3.Row]] = {}
    for chunk in chunks:
        by_document.setdefault(str(chunk["document_id"]), []).append(chunk)
    for document in documents:
        module_id = str(document["module_id"] if "module_id" in document.keys() else "local-ai")
        legacy_document_id = str(document["document_id"])
        resource_id = f"doc-{legacy_document_id}"
        label = f"local-model-platform:{module_id}:business:document:{resource_id}"
        locator_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{module_id}:{document['source']}")
        document_chunks = by_document.get(legacy_document_id, [])
        metadata = {
            "document_id": legacy_document_id,
            "title": document["title"],
            "character_count": document["character_count"],
            "chunk_count": len(document_chunks),
            "embedding_model": document["embedding_model"],
        }
        connection.execute(
            """INSERT INTO gptbridge_index.resource
            (resource_id,platform_id,module_id,owner_id,data_category,resource_type,
             resource_label,classification,locator_id,content_hash,index_status,metadata)
            VALUES (%s,'local-model-platform',%s,%s,'business','document',%s,'private',%s,%s,'indexed',%s)
            ON CONFLICT (resource_id) DO UPDATE SET locator_id=EXCLUDED.locator_id,
              content_hash=EXCLUDED.content_hash,index_status='indexed',metadata=EXCLUDED.metadata""",
            (resource_id, module_id, module_id, label, locator_id, document["sha256"], json.dumps(metadata, ensure_ascii=False)),
        )
        for chunk in document_chunks:
            chunk_id = str(chunk["chunk_id"])
            point_id = uuid.uuid5(uuid.NAMESPACE_URL, "gptbridge-rag:" + chunk_id)
            chunk_label = f"local-model-platform:{module_id}:business:chunk:chunk-{chunk_id}"
            connection.execute(
                """INSERT INTO gptbridge_rag.chunk
                (chunk_id,resource_id,module_id,sequence,character_start,character_end,
                 qdrant_point_id,embedding_model,title,content,metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (chunk_id) DO UPDATE SET content=EXCLUDED.content,metadata=EXCLUDED.metadata""",
                (chunk_id, resource_id, module_id, chunk["sequence"], chunk["character_start"],
                 chunk["character_end"], point_id, document["embedding_model"], document["title"],
                 chunk["content"], json.dumps({"resource_label": chunk_label})),
            )
    migrated = connection.execute("SELECT count(*) FROM gptbridge_rag.chunk").fetchone()[0]
    if int(migrated) < len(chunks):
        raise RuntimeError("RAG_MIGRATION_COUNT_MISMATCH")
    return len(chunks)


def main() -> int:
    if not CENTRAL_SCHEMA.is_file():
        raise RuntimeError("CENTRAL_INDEX_SCHEMA_REQUIRED")
    with psycopg.connect(_dsn()) as connection:
        connection.execute(CENTRAL_SCHEMA.read_text(encoding="utf-8"))
        channel_counts = {
            channel: _migrate_channel(connection, channel, database)
            for channel, database in CHANNELS.items()
        }
        rag_chunks = _migrate_rag(connection)
    cleanup = [_remove_sqlite_family(database) for database in CHANNELS.values()]
    cleanup.append(_remove_sqlite_family(RAG_DATABASE))
    print(json.dumps({"ok": True, "channels": channel_counts, "rag_chunks": rag_chunks, "legacy_deleted": all(cleanup)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
