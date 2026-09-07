"""Bounded local vector cache for degraded RAG operation.

Qdrant remains the canonical semantic index. Vectors persisted to local SQLite
provide an observable tool-private cache while Qdrant is unavailable and must
not be treated as cross-module semantic authority. Two point styles are accepted:

* external vectors — points carry ``vector`` (e.g. an Ollama embedding model,
  the retained local official service); vectors are stored as given, cosine
  scoring is normalized at query time;
* text embeddings — points carry ``text`` (backward compatible with the old
  in-memory store); they are embedded locally through hashing-based character
  n-gram vectors via ``embed_vector`` / ``_token_vector``.

No numpy/scipy/third-party (A37/E23).  A C++ native kernel hook
(``local.native_kernel``) may accelerate the hot vector math when the compiled
``.pyd`` exists; when it does not, a pure Python fallback is used.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterator

from .native_kernel import available as _native_available

COLLECTION: Final[str] = "gptbridge_shared_knowledge"
DEFAULT_ENDPOINT: Final[str] = "local"

_TOKENS: Final[re.Pattern[str]] = re.compile(r"[a-z0-9\u4e00-\u9fff]+")
_DIMENSION: Final[int] = 256
_NGRAM: Final[int] = 3
_MAX_POINTS: Final[int] = 100_000


@dataclass(frozen=True)
class _Point:
    id: str
    document_id: str
    module_id: str
    vector: tuple[float, ...]
    payload: dict[str, Any]


def _token_vector(text: str, dimension: int = _DIMENSION) -> list[float]:
    vector = [0.0] * dimension
    for token in _TOKENS.findall(str(text).lower()):
        for end in range(_NGRAM, len(token) + 1):
            gram = token[end - _NGRAM:end]
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % dimension
            weight = float(int.from_bytes(digest[4:], "little")) / float(2**64 - 1) + 1.0
            vector[index] += weight
    return vector


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0 or not math.isfinite(norm):
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def embed_vector(text: str, dimension: int = _DIMENSION) -> list[float]:
    """Local hashing n-gram embedding; normalized, stdlib-only."""
    return _normalize(_token_vector(text, dimension))


class LocalVectorStore:
    """Tool-private SQLite vector cache for bounded degraded retrieval.

    Qdrant remains canonical. Modules may cache embeddings locally for
    continuity, but these candidates are non-authoritative and must be
    reconciled through the governed RAG path before canonical use.
    """

    DEFAULT_ENDPOINT = DEFAULT_ENDPOINT
    COLLECTION = COLLECTION

    def __init__(
        self,
        root: Path | str,
        *,
        dimension: int = _DIMENSION,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.endpoint = "local" if endpoint in {"", "local"} else str(endpoint).rstrip("/")
        self._dimension = int(dimension)
        self._native = _native_available()
        path = Path(root).resolve()
        if path.is_dir():
            path = path / "local-rag-vectors.sqlite3"
        self.database_path = path
        self.location = str(path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collection_meta (
                    document_id TEXT NOT NULL PRIMARY KEY,
                    module_id TEXT NOT NULL,
                    vector_size INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS collection_point (
                    point_id TEXT NOT NULL PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _loads(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def ensure_collection(self, vector_size: int) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(vector_size) AS size FROM collection_meta"
            ).fetchone()
        existing = int(row["size"]) if row is not None and row["size"] is not None else None
        if existing is not None and int(vector_size) != existing:
            raise RuntimeError(
                f"RAG_VECTOR_DIMENSION_MISMATCH: existing={existing}, requested={vector_size}"
            )

    def replace_document(
        self,
        document_id: str,
        points: list[dict[str, Any]],
        *,
        module_id: str | None = None,
    ) -> None:
        resolved_module = str(module_id or "")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM collection_point WHERE document_id = ?",
                (document_id,),
            )
            connection.execute(
                "DELETE FROM collection_meta WHERE document_id = ?",
                (document_id,),
            )
            if not points:
                return
            prepared: list[tuple[str, str, str, str, str]] = []
            vector_size = 0
            for point in points[: _MAX_POINTS]:
                point_id = str(point.get("id") or point.get("point_id") or "")
                if not point_id:
                    raise ValueError("RAG_POINT_ID_REQUIRED")
                raw_vector = point.get("vector")
                if isinstance(raw_vector, (list, tuple)) and raw_vector:
                    vector = [float(value) for value in raw_vector]
                elif str(point.get("text") or "").strip():
                    vector = _token_vector(str(point["text"]), self._dimension)
                else:
                    raise ValueError("RAG_POINT_VECTOR_OR_TEXT_REQUIRED")
                vector = _normalize(vector)
                if not vector_size:
                    vector_size = len(vector)
                point_module = str(point.get("module_id") or resolved_module)
                payload = dict(point.get("payload") or {})
                payload["module_id"] = str(payload.get("module_id") or point_module)
                payload["document_id"] = str(payload.get("document_id") or document_id)
                prepared.append(
                    (
                        point_id,
                        document_id,
                        point_module,
                        json.dumps(vector),
                        json.dumps(payload, ensure_ascii=False),
                    )
                )
            connection.execute(
                """
                INSERT INTO collection_meta (
                    document_id, module_id, vector_size
                ) VALUES (?, ?, ?)
                ON CONFLICT (document_id) DO UPDATE SET
                    module_id = excluded.module_id,
                    vector_size = excluded.vector_size
                """,
                (document_id, resolved_module or str(points[0].get("module_id") or ""), vector_size),
            )
            connection.executemany(
                """
                INSERT INTO collection_point (
                    point_id, document_id, module_id, vector, payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                prepared,
            )

    def query(
        self,
        vector: list[float],
        *,
        limit: int,
        module_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        query_vector = _normalize([float(value) for value in vector])
        with self._connect() as connection:
            if module_ids:
                placeholders = ", ".join("?" for _ in module_ids)
                rows = connection.execute(
                    f"""
                    SELECT point_id, document_id, module_id, vector, payload
                    FROM collection_point
                    WHERE module_id IN ({placeholders})
                    """,
                    tuple(module_ids),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT point_id, document_id, module_id, vector, payload FROM collection_point"
                ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            stored = self._loads(row["vector"])
            if not isinstance(stored, list) or len(stored) != len(query_vector):
                continue
            score = _cosine(query_vector, [float(value) for value in stored])
            payload = self._loads(row["payload"])
            payload = payload if isinstance(payload, dict) else {}
            record: dict[str, Any] = {
                **payload,
                "point_id": str(row["point_id"]),
                "document_id": str(row["document_id"]),
                "module_id": str(row["module_id"]),
                "vector_score": round(score, 6),
                "id": str(row["point_id"]),
                "score": round(score, 6),
            }
            scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for score, record in scored[: max(0, int(limit))]]

    def status(self) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                point_row = connection.execute(
                    "SELECT COUNT(*) AS count FROM collection_point"
                ).fetchone()
                meta_row = connection.execute(
                    "SELECT COUNT(*) AS docs, MAX(vector_size) AS size FROM collection_meta"
                ).fetchone()
            point_count = int(point_row["count"])
            return {
                "available": True,
                "engine": "local-vector-degraded-cache",
                "canonical": False,
                "reconciliation_required": True,
                "native": self._native,
                "dimension": self._dimension,
                "points": point_count,
                "collection_exists": point_count > 0,
                "endpoint": self.endpoint,
                "collection": self.COLLECTION,
                "point_count": point_count,
                "document_count": int(meta_row["docs"]),
                "vector_size": int(meta_row["size"]) if meta_row["size"] is not None else None,
                "location": self.location,
            }
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {
                "available": False,
                "engine": "local-vector-degraded-cache",
                "canonical": False,
                "reconciliation_required": True,
                "native": self._native,
                "dimension": self._dimension,
                "points": 0,
                "collection_exists": False,
                "endpoint": self.endpoint,
                "collection": self.COLLECTION,
                "point_count": 0,
                "document_count": 0,
                "vector_size": None,
                "location": self.location,
                "last_error": str(exc)[:500],
            }

    def reindex(
        self, document_id: str, points: list[dict[str, Any]], *, module_id: str | None = None
    ) -> None:
        self.replace_document(document_id, points, module_id=module_id)

    def delete(self, document_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM collection_point WHERE document_id = ?",
                (document_id,),
            )
            connection.execute(
                "DELETE FROM collection_meta WHERE document_id = ?",
                (document_id,),
            )


VectorStore = LocalVectorStore

__all__ = ["LocalVectorStore", "VectorStore", "embed_vector"]