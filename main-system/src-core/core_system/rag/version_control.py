"""RAG Version Control & Embedding Cache — 併發控制與重用機制。

PostgreSQL 為版本判定權威；Embedding Cache 避免重複計算。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

_logger = logging.getLogger("gptbridge.rag.version")


class ResourceState(str, Enum):
    """Resource lifecycle states."""
    SOURCE = "SOURCE"
    PARSED = "PARSED"
    CHUNKED = "CHUNKED"
    EMBEDDED = "EMBEDDED"
    INDEX_PENDING = "INDEX_PENDING"
    CANONICAL_INDEXED = "CANONICAL_INDEXED"
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    TOMBSTONED = "TOMBSTONED"
    DEGRADED_PENDING = "DEGRADED_PENDING"
    RECONCILING = "RECONCILING"


class KnowledgeKind(str, Enum):
    """Knowledge classification."""
    SOURCE = "SOURCE"           # 原始資料 (PDF, code, docs)
    DERIVED = "DERIVED"         # AI 生成 (摘要, 結論, 知識條目)
    MEMORY = "MEMORY"           # 使用者上下文、會話記憶
    GENERATED = "GENERATED"     # 衍生計算結果


@dataclass(frozen=True)
class ResourceVersion:
    """Resource version record."""
    resource_id: str
    module_id: str
    version: int
    content_hash: str
    generation_id: str
    knowledge_kind: KnowledgeKind
    state: ResourceState
    chunk_policy_version: str
    chunk_count: int
    embedding_model: str
    embedding_dimension: int
    updated_at: str
    created_at: str
    previous_version: Optional[int] = None
    source_version: Optional[int] = None  # For DERIVED: points to source version


@dataclass(frozen=True)
class EmbeddingCacheEntry:
    """Embedding cache entry."""
    cache_id: str
    content_hash: str
    embedding_model: str
    embedding_dimension: int
    vector: list[float]
    created_at: str
    hit_count: int = 0


@dataclass(frozen=True)
class ChunkRecord:
    """Stable chunk record with deterministic ID."""
    chunk_id: str
    resource_id: str
    module_id: str
    generation_id: str
    content: str
    content_hash: str
    sequence: int
    character_start: int
    character_end: int
    chunk_policy_version: str
    version: int
    state: ResourceState
    qdrant_point_id: Optional[str] = None
    embedding_cached: bool = False


@dataclass(frozen=True)
class DiffResult:
    """Result of chunk diff between old and new."""
    resource_id: str
    kept: list[ChunkRecord]      # Unchanged chunks
    added: list[ChunkRecord]     # New chunks
    updated: list[ChunkRecord]   # Changed chunks (new version)
    deleted: list[ChunkRecord]   # Removed chunks


# PostgreSQL Schema
VERSION_CONTROL_SQL = """
-- Resource version table (authoritative version control)
CREATE TABLE IF NOT EXISTS rag_resource_versions (
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    knowledge_kind TEXT NOT NULL DEFAULT 'SOURCE',
    state TEXT NOT NULL DEFAULT 'SOURCE',
    chunk_policy_version TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    previous_version INTEGER,
    source_version INTEGER,
    PRIMARY KEY (resource_id, module_id, version)
);

CREATE INDEX IF NOT EXISTS idx_rag_resource_versions_latest
ON rag_resource_versions (resource_id, module_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_rag_resource_versions_state
ON rag_resource_versions (state, generation_id);

-- Current version view (for fast lookup)
CREATE OR REPLACE VIEW rag_current_versions AS
SELECT DISTINCT ON (resource_id, module_id)
    resource_id, module_id, version, content_hash, generation_id,
    knowledge_kind, state, chunk_policy_version, chunk_count,
    embedding_model, embedding_dimension, updated_at_utc, created_at_utc,
    previous_version, source_version
FROM rag_resource_versions
ORDER BY resource_id, module_id, version DESC;

-- Embedding cache table
CREATE TABLE IF NOT EXISTS rag_embedding_cache (
    cache_id UUID PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    vector BYTEA NOT NULL,  -- Store as binary for efficiency
    created_at_utc TEXT NOT NULL,
    hit_count INTEGER DEFAULT 0,
    UNIQUE (content_hash, embedding_model, embedding_dimension)
);

CREATE INDEX IF NOT EXISTS idx_rag_embedding_cache_lookup
ON rag_embedding_cache (content_hash, embedding_model, embedding_dimension);

-- Stable chunk table
CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    character_start INTEGER NOT NULL,
    character_end INTEGER NOT NULL,
    chunk_policy_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'CHUNKED',
    qdrant_point_id TEXT,
    embedding_cached BOOLEAN DEFAULT FALSE,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_resource
ON rag_chunks (resource_id, module_id, generation_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_content_hash
ON rag_chunks (content_hash);

-- Provenance table for DERIVED knowledge
CREATE TABLE IF NOT EXISTS rag_provenance (
    derived_resource_id TEXT NOT NULL,
    derived_chunk_id TEXT NOT NULL,
    source_resource_id TEXT NOT NULL,
    source_chunk_id TEXT NOT NULL,
    source_generation_id TEXT NOT NULL,
    generation_model TEXT NOT NULL,
    generated_at_utc TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (derived_resource_id, derived_chunk_id, source_resource_id, source_chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_provenance_source
ON rag_provenance (source_resource_id, source_chunk_id);
"""


class VersionController:
    """PostgreSQL-based optimistic locking version controller."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def get_current_version(self, module_id: str, resource_id: str) -> Optional[ResourceVersion]:
        """Get current version of resource (authoritative)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT resource_id, module_id, version, content_hash, generation_id,
                       knowledge_kind, state, chunk_policy_version, chunk_count,
                       embedding_model, embedding_dimension, updated_at_utc, created_at_utc,
                       previous_version, source_version
                FROM rag_current_versions
                WHERE resource_id = %s AND module_id = %s
                """,
                (resource_id, module_id),
            )
            row = cur.fetchone()
            if row:
                return ResourceVersion(
                    resource_id=row["resource_id"],
                    module_id=row["module_id"],
                    version=row["version"],
                    content_hash=row["content_hash"],
                    generation_id=row["generation_id"],
                    knowledge_kind=KnowledgeKind(row["knowledge_kind"]),
                    state=ResourceState(row["state"]),
                    chunk_policy_version=row["chunk_policy_version"],
                    chunk_count=row["chunk_count"],
                    embedding_model=row["embedding_model"],
                    embedding_dimension=row["embedding_dimension"],
                    updated_at=row["updated_at_utc"],
                    created_at=row["created_at_utc"],
                    previous_version=row["previous_version"],
                    source_version=row["source_version"],
                )
        return None

    def try_update_version(
        self,
        module_id: str,
        resource_id: str,
        expected_version: int,
        new_content_hash: str,
        new_generation_id: str,
        knowledge_kind: KnowledgeKind,
        new_state: ResourceState,
        chunk_policy_version: str,
        chunk_count: int,
        embedding_model: str,
        embedding_dimension: int,
    ) -> tuple[bool, Optional[ResourceVersion]]:
        """Optimistic lock update: only succeeds if current version == expected_version.

        Returns (success, new_version_record).
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # Lock the current version row
                cur.execute(
                    """
                    SELECT version FROM rag_resource_versions
                    WHERE resource_id = %s AND module_id = %s AND version = %s
                    FOR UPDATE
                    """,
                    (resource_id, module_id, expected_version),
                )
                if not cur.fetchone():
                    return False, None  # Version mismatch or not found

                # Insert new version
                new_version = expected_version + 1
                now = datetime.now(timezone.utc).isoformat()
                cur.execute(
                    """
                    INSERT INTO rag_resource_versions
                    (resource_id, module_id, version, content_hash, generation_id,
                     knowledge_kind, state, chunk_policy_version, chunk_count,
                     embedding_model, embedding_dimension, updated_at_utc, created_at_utc,
                     previous_version, source_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resource_id, module_id, new_version, new_content_hash, new_generation_id,
                        knowledge_kind.value, new_state.value, chunk_policy_version, chunk_count,
                        embedding_model, embedding_dimension, now, now,
                        expected_version, None,
                    ),
                )
                conn.commit()

                new_record = ResourceVersion(
                    resource_id=resource_id,
                    module_id=module_id,
                    version=new_version,
                    content_hash=new_content_hash,
                    generation_id=new_generation_id,
                    knowledge_kind=knowledge_kind,
                    state=new_state,
                    chunk_policy_version=chunk_policy_version,
                    chunk_count=chunk_count,
                    embedding_model=embedding_model,
                    embedding_dimension=embedding_dimension,
                    updated_at=now,
                    created_at=now,
                    previous_version=expected_version,
                    source_version=None,
                )
                return True, new_record

        except Exception as e:
            conn.rollback()
            _logger.error("VersionController: try_update_version failed: %s", e)
            return False, None

    def insert_initial_version(
        self,
        module_id: str,
        resource_id: str,
        content_hash: str,
        generation_id: str,
        knowledge_kind: KnowledgeKind,
        state: ResourceState,
        chunk_policy_version: str,
        chunk_count: int,
        embedding_model: str,
        embedding_dimension: int,
    ) -> ResourceVersion:
        """Insert first version of a new resource."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_resource_versions
                (resource_id, module_id, version, content_hash, generation_id,
                 knowledge_kind, state, chunk_policy_version, chunk_count,
                 embedding_model, embedding_dimension, updated_at_utc, created_at_utc,
                 previous_version, source_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (resource_id, module_id, version) DO NOTHING
                RETURNING version
                """,
                (
                    resource_id, module_id, 1, content_hash, generation_id,
                    knowledge_kind.value, state.value, chunk_policy_version, chunk_count,
                    embedding_model, embedding_dimension, now, now,
                    None, None,
                ),
            )
            conn.commit()
        return ResourceVersion(
            resource_id=resource_id,
            module_id=module_id,
            version=1,
            content_hash=content_hash,
            generation_id=generation_id,
            knowledge_kind=knowledge_kind,
            state=state,
            chunk_policy_version=chunk_policy_version,
            chunk_count=chunk_count,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            updated_at=now,
            created_at=now,
            previous_version=None,
            source_version=None,
        )


class EmbeddingCache:
    """Content-hash-based embedding cache."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def _cache_key(self, content_hash: str, model: str, dimension: int) -> str:
        return f"{content_hash}:{model}:{dimension}"

    def get(self, content_hash: str, model: str, dimension: int) -> Optional[list[float]]:
        """Get cached embedding vector."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT vector, hit_count FROM rag_embedding_cache
                WHERE content_hash = %s AND embedding_model = %s AND embedding_dimension = %s
                """,
                (content_hash, model, dimension),
            )
            row = cur.fetchone()
            if row:
                # Increment hit count
                cur.execute(
                    "UPDATE rag_embedding_cache SET hit_count = hit_count + 1 WHERE cache_id = %s",
                    (row["cache_id"],),
                )
                # Deserialize vector (stored as BYTEA)
                import pickle
                return pickle.loads(row["vector"])
        return None

    def put(self, content_hash: str, model: str, dimension: int, vector: list[float]) -> str:
        """Store embedding in cache."""
        conn = self._get_conn()
        import pickle
        cache_id = uuid.uuid4()
        vector_bytes = pickle.dumps(vector)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_embedding_cache
                (cache_id, content_hash, embedding_model, embedding_dimension, vector, created_at_utc, hit_count)
                VALUES (%s, %s, %s, %s, %s, %s, 0)
                ON CONFLICT (content_hash, embedding_model, embedding_dimension) DO NOTHING
                """,
                (cache_id, content_hash, model, dimension, vector_bytes, datetime.now(timezone.utc).isoformat()),
            )
        return str(cache_id)

    def get_or_compute(
        self,
        content: str,
        model: str,
        dimension: int,
        embed_fn: callable,
    ) -> tuple[list[float], bool]:
        """Get from cache or compute and cache.

        Returns (vector, from_cache).
        """
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        cached = self.get(content_hash, model, dimension)
        if cached is not None:
            return cached, True

        # Compute
        vector = embed_fn(content)
        self.put(content_hash, model, dimension, vector)
        return vector, False


class ChunkManager:
    """Manages stable chunks with deterministic IDs."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    @staticmethod
    def deterministic_chunk_id(
        resource_id: str,
        content: str,
        chunk_policy_version: str,
    ) -> str:
        """Generate stable chunk ID from content hash + resource + policy."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        policy_hash = hashlib.sha256(chunk_policy_version.encode("utf-8")).hexdigest()[:8]
        combined = f"{resource_id}:{content_hash}:{policy_hash}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:24]

    def diff_chunks(
        self,
        resource_id: str,
        module_id: str,
        generation_id: str,
        new_chunks: list[tuple[str, int, int, str]],  # (content, char_start, char_end, policy_version)
        embedding_model: str,
        embedding_dimension: int,
    ) -> DiffResult:
        """Compare new chunks with existing chunks, return diff."""
        conn = self._get_conn()

        # Fetch existing chunks for this resource+generation
        existing = {}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id, content_hash, sequence, character_start, character_end,
                       chunk_policy_version, version, state, qdrant_point_id, embedding_cached
                FROM rag_chunks
                WHERE resource_id = %s AND module_id = %s AND generation_id = %s
                ORDER BY sequence
                """,
                (resource_id, module_id, generation_id),
            )
            for row in cur.fetchall():
                existing[row["content_hash"]] = ChunkRecord(
                    chunk_id=row["chunk_id"],
                    resource_id=resource_id,
                    module_id=module_id,
                    generation_id=generation_id,
                    content="",  # Not needed for diff
                    content_hash=row["content_hash"],
                    sequence=row["sequence"],
                    character_start=row["character_start"],
                    character_end=row["character_end"],
                    chunk_policy_version=row["chunk_policy_version"],
                    version=row["version"],
                    state=ResourceState(row["state"]),
                    qdrant_point_id=row["qdrant_point_id"],
                    embedding_cached=row["embedding_cached"],
                )

        # Build new chunk records
        kept = []
        added = []
        updated = []
        deleted = []

        new_content_hashes = set()
        for i, (content, char_start, char_end, policy_version) in enumerate(new_chunks):
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            new_content_hashes.add(content_hash)
            chunk_id = self.deterministic_chunk_id(resource_id, content, policy_version)

            if content_hash in existing:
                old = existing[content_hash]
                if old.chunk_policy_version == policy_version:
                    # Unchanged - KEEP
                    kept.append(ChunkRecord(
                        chunk_id=old.chunk_id,
                        resource_id=resource_id,
                        module_id=module_id,
                        generation_id=generation_id,
                        content=content,
                        content_hash=content_hash,
                        sequence=i,
                        character_start=char_start,
                        character_end=char_end,
                        chunk_policy_version=policy_version,
                        version=old.version,
                        state=ResourceState.CHUNKED,
                        qdrant_point_id=old.qdrant_point_id,
                        embedding_cached=old.embedding_cached,
                    ))
                else:
                    # Policy changed - UPDATE
                    updated.append(ChunkRecord(
                        chunk_id=chunk_id,
                        resource_id=resource_id,
                        module_id=module_id,
                        generation_id=generation_id,
                        content=content,
                        content_hash=content_hash,
                        sequence=i,
                        character_start=char_start,
                        character_end=char_end,
                        chunk_policy_version=policy_version,
                        version=old.version + 1,
                        state=ResourceState.CHUNKED,
                        qdrant_point_id=None,
                        embedding_cached=False,
                    ))
            else:
                # New chunk - ADD
                added.append(ChunkRecord(
                    chunk_id=chunk_id,
                    resource_id=resource_id,
                    module_id=module_id,
                    generation_id=generation_id,
                    content=content,
                    content_hash=content_hash,
                    sequence=i,
                    character_start=char_start,
                    character_end=char_end,
                    chunk_policy_version=policy_version,
                    version=1,
                    state=ResourceState.CHUNKED,
                    qdrant_point_id=None,
                    embedding_cached=False,
                ))

        # Find deleted chunks
        for old in existing.values():
            if old.content_hash not in new_content_hashes:
                deleted.append(old)

        return DiffResult(
            resource_id=resource_id,
            kept=kept,
            added=added,
            updated=updated,
            deleted=deleted,
        )

    def upsert_chunks(self, chunks: list[ChunkRecord]) -> None:
        """Upsert chunk records to PostgreSQL."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        with conn.cursor() as cur:
            for chunk in chunks:
                cur.execute(
                    """
                    INSERT INTO rag_chunks
                    (chunk_id, resource_id, module_id, generation_id, content, content_hash,
                     sequence, character_start, character_end, chunk_policy_version,
                     version, state, qdrant_point_id, embedding_cached,
                     created_at_utc, updated_at_utc)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        content = EXCLUDED.content,
                        content_hash = EXCLUDED.content_hash,
                        sequence = EXCLUDED.sequence,
                        character_start = EXCLUDED.character_start,
                        character_end = EXCLUDED.character_end,
                        chunk_policy_version = EXCLUDED.chunk_policy_version,
                        version = EXCLUDED.version,
                        state = EXCLUDED.state,
                        qdrant_point_id = EXCLUDED.qdrant_point_id,
                        embedding_cached = EXCLUDED.embedding_cached,
                        updated_at_utc = EXCLUDED.updated_at_utc
                    """,
                    (
                        chunk.chunk_id, chunk.resource_id, chunk.module_id, chunk.generation_id,
                        chunk.content, chunk.content_hash, chunk.sequence,
                        chunk.character_start, chunk.character_end, chunk.chunk_policy_version,
                        chunk.version, chunk.state.value, chunk.qdrant_point_id,
                        chunk.embedding_cached, now, now,
                    ),
                )


class ProvenanceTracker:
    """Tracks provenance for DERIVED knowledge."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def record_derivation(
        self,
        derived_resource_id: str,
        derived_chunk_id: str,
        source_resource_id: str,
        source_chunk_id: str,
        source_generation_id: str,
        generation_model: str,
        content_hash: str,
    ) -> None:
        """Record that derived chunk was generated from source chunks."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_provenance
                (derived_resource_id, derived_chunk_id, source_resource_id,
                 source_chunk_id, source_generation_id, generation_model,
                 generated_at_utc, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    derived_resource_id, derived_chunk_id, source_resource_id,
                    source_chunk_id, source_generation_id, generation_model,
                    datetime.now(timezone.utc).isoformat(), content_hash,
                ),
            )

    def get_sources(self, derived_resource_id: str, derived_chunk_id: str) -> list[dict]:
        """Get all source chunks for a derived chunk."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_resource_id, source_chunk_id, source_generation_id,
                       generation_model, generated_at_utc, content_hash
                FROM rag_provenance
                WHERE derived_resource_id = %s AND derived_chunk_id = %s
                """,
                (derived_resource_id, derived_chunk_id),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_stale(self, source_resource_id: str, new_source_version: int) -> list[dict]:
        """Find all derived knowledge that is now stale due to source update.

        Returns list of stale derived resources/chunks.
        """
        conn = self._get_conn()
        stale = []
        with conn.cursor() as cur:
            # Find derived chunks pointing to old version of source
            cur.execute(
                """
                SELECT DISTINCT derived_resource_id, derived_chunk_id
                FROM rag_provenance
                WHERE source_resource_id = %s
                """,
                (source_resource_id,),
            )
            for row in cur.fetchall():
                stale.append(dict(row))
        return stale


__all__ = [
    "ResourceState",
    "KnowledgeKind",
    "ResourceVersion",
    "EmbeddingCacheEntry",
    "ChunkRecord",
    "DiffResult",
    "VERSION_CONTROL_SQL",
    "VersionController",
    "EmbeddingCache",
    "ChunkManager",
    "ProvenanceTracker",
]