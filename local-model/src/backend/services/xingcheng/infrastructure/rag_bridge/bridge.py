from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


@dataclass(frozen=True)
class LocalHit:
    resource_id: str
    chunk_id: str
    module_id: str
    score: float


class VectorStore(Protocol):
    def ensure_collection(self, vector_size: int) -> None: ...
    def replace_document(self, document_id: str, points: list[dict[str, Any]], *, module_id: str | None = None) -> None: ...
    def query(self, vector: list[float], *, limit: int, module_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]: ...
    def status(self) -> dict[str, Any]: ...


class RagAuthorizationBridge:
    """Treat index hits as candidates, never as authorization.

    Codex basis: A10/E10 (deny-by-default, explicit-allow) — vector matches
    grant no access; every hit must pass the authorizer callback.
    """

    def __init__(self, authorizer: Callable[[str, str], bool]) -> None:
        self._authorizer = authorizer

    def filter_authorized(self, actor_id: str, hits: Iterable[LocalHit]) -> tuple[LocalHit, ...]:
        return tuple(hit for hit in hits if self._authorizer(actor_id, hit.resource_id))


class RagIndexCoordinator:
    """Local index lifecycle plus mapping; vector matches grant no access.

    Codex basis: A8 (Qdrant=canonical semantic index; local-vector=degraded
    cache only), A44 (fallback bounded+observable+reconciled), A10 (index
    hits are candidates not authorization).  The forbidden-payload check
    enforces A11 (fail-closed) — physical paths/content must not leak into
    index payloads.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        save_mapping: Callable[[str, str, str], None],
        delete_mapping: Callable[[str], None],
    ) -> None:
        self.vector_store = vector_store
        self._save_mapping = save_mapping
        self._delete_mapping = delete_mapping

    def ensure_collection(self, vector_size: int) -> None:
        self.vector_store.ensure_collection(vector_size)

    def upsert(self, resource_id: str, module_id: str, points: list[dict[str, Any]]) -> None:
        forbidden = {"content", "text", "physical_location", "path", "windows_path"}
        for point in points:
            payload = point.get("payload") or {}
            if forbidden.intersection(str(key).casefold() for key in payload):
                raise ValueError("RAG_PAYLOAD_MUST_NOT_CONTAIN_PHYSICAL_CONTENT_OR_PATH")
        self.vector_store.replace_document(resource_id, points, module_id=module_id)
        for point in points:
            payload = point.get("payload") or {}
            self._save_mapping(str(payload["chunk_id"]), resource_id, str(point["id"]))

    def delete(self, resource_id: str, module_id: str) -> None:
        self.vector_store.replace_document(resource_id, [], module_id=module_id)
        self._delete_mapping(resource_id)

    def reindex(self, resource_id: str, module_id: str, points: list[dict[str, Any]]) -> None:
        self.delete(resource_id, module_id)
        self.upsert(resource_id, module_id, points)

    def health(self) -> dict[str, Any]:
        return self.vector_store.status()


# QdrantHit is a type alias for LocalHit — both represent index *candidates*
# that must pass authorization filtering before use.  The alias exists so
# callers that conceptually target Qdrant (the canonical semantic index, A8)
# can use a descriptive name, but the runtime object is the same: a
# non-authoritative hit from whatever index backend is active (Qdrant when
# canonical, or LocalVectorStore when in A44 degraded-fallback mode).
# Index hits NEVER grant access — RagAuthorizationBridge enforces this (A10).
QdrantHit = LocalHit

__all__ = ["LocalHit", "QdrantHit", "RagAuthorizationBridge", "RagIndexCoordinator", "VectorStore"]