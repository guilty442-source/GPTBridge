from __future__ import annotations

"""migrate_legacy_indexes_to_local — codex-native legacy index migration.

Replaces the retired ``migrate_legacy_indexes_to_postgresql.py``.  Migrates
any legacy sqlite channel/RAG databases straight into the local governed
sqlite stores (A44/E30).  No PostgreSQL/psycopg, no external service.
"""

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
from shared_layer.resource_identity import point_id_for


ROOT = Path(__file__).resolve().parents[2]
LEGACY_CHANNELS = {
    "system": ROOT / "shared-layer" / "data" / "system-channel.sqlite3",
    "ai": ROOT / "shared-layer" / "data" / "ai-channel.sqlite3",
}
LEGACY_RAG = ROOT / "local-model" / "runtime" / "state" / "local-rag-keywords.sqlite3"
TARGET_CHANNELS = {
    "system": ROOT / "shared-layer" / "runtime" / "system.sqlite3",
    "ai": ROOT / "shared-layer" / "runtime" / "ai.sqlite3",
}
TARGET_CENTRAL = ROOT / "shared-layer" / "runtime" / "central-index.sqlite3"


def _ensure_schema_target(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS tool_request (
            channel_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            requester_actor TEXT NOT NULL,
            target_tool_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            response TEXT,
            progress TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, request_id)
        )"""
    )


def _migrate_channel(channel: str, legacy: Path, target: Path) -> int:
    if not legacy.is_file():
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(legacy, uri=True) as source:
        source.row_factory = sqlite3.Row
        try:
            rows = source.execute("SELECT * FROM tool_requests").fetchall()
        except sqlite3.DatabaseError:
            return 0
    with sqlite3.connect(target) as destination:
        _ensure_schema_target(destination)
        count = 0
        for row in rows:
            destination.execute(
                """INSERT OR REPLACE INTO tool_request
                (channel_id,request_id,requester_actor,target_tool_id,payload,status,response,progress,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    channel,
                    row["request_id"],
                    row["requester_actor"],
                    row["target_tool_id"],
                    row["payload_json"],
                    row["status"],
                    row["response_json"],
                    row["progress_json"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            count += 1
    return count


def _migrate_rag(legacy: Path) -> int:
    if not legacy.is_file():
        return 0
    with sqlite3.connect(legacy, uri=True) as source:
        source.row_factory = sqlite3.Row
        try:
            chunks = source.execute("SELECT * FROM rag_chunk ORDER BY document_id, sequence").fetchall()
        except sqlite3.DatabaseError:
            return 0
    central = TARGET_CENTRAL
    central.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(central) as destination:
        destination.execute(
            """CREATE TABLE IF NOT EXISTS rag_chunk (
                chunk_id TEXT NOT NULL PRIMARY KEY,
                resource_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                character_start INTEGER NOT NULL,
                character_end INTEGER NOT NULL,
                point_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT
            )"""
        )
        count = 0
        for chunk in chunks:
            chunk_id = str(chunk["chunk_id"])
            resource_id = f"doc-{chunk['document_id']}"
            module_id = str(chunk["module_id"]) if "module_id" in chunk.keys() else "xingcheng"
            point_id = str(point_id_for(chunk_id))
            destination.execute(
                """INSERT OR REPLACE INTO rag_chunk
                (chunk_id,resource_id,module_id,sequence,character_start,character_end,point_id,embedding_model,title,content,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk_id, resource_id, module_id, chunk["sequence"],
                    chunk["character_start"], chunk["character_end"], point_id,
                    chunk["embedding_model"], chunk["title"], chunk["content"],
                    json.dumps({"resource_id": resource_id}, ensure_ascii=False),
                ),
            )
            count += 1
    return count


def _remove_legacy(database: Path) -> bool:
    removed = True
    for candidate in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        if candidate.exists():
            try:
                candidate.unlink()
            except PermissionError:
                removed = False
    return removed


def main() -> int:
    channel_counts = {
        channel: _migrate_channel(channel, legacy, TARGET_CHANNELS[channel])
        for channel, legacy in LEGACY_CHANNELS.items()
    }
    rag_chunks = _migrate_rag(LEGACY_RAG)
    cleanup = [_remove_legacy(database) for database in LEGACY_CHANNELS.values()]
    cleanup.append(_remove_legacy(LEGACY_RAG))
    print(json.dumps({
        "ok": True,
        "channels": channel_counts,
        "rag_chunks": rag_chunks,
        "legacy_deleted": all(cleanup),
        "deprecated_postgresql": True,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())