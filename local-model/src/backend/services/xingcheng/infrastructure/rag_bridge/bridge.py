from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass
class QdrantHit:
    """Single vector hit from Qdrant."""

    resource_id: str
    chunk_id: str
    tool_id: str
    score: float


class RagAuthorizationBridge:
    """Filter RAG hits by governed authorization."""

    def __init__(self, authorize: Callable[[str, str], bool]) -> None:
        self.authorize = authorize

    def filter_authorized(
        self,
        actor: str,
        hits: Iterable[QdrantHit],
    ) -> list[QdrantHit]:
        return [hit for hit in hits if self.authorize(actor, hit.resource_id)]


class RagIndexCoordinator:
    """Coordinate RAG index writes while forbidding physical content payloads."""

    def __init__(
        self,
        store: Any,
        index_loader: Callable[..., Any] | None = None,
        vector_loader: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store
        self.index_loader = index_loader
        self.vector_loader = vector_loader

    def _validate_payload(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("RAG_PAYLOAD_MUST_NOT_CONTAIN_PHYSICAL_CONTENT_OR_PATH")
        for key, value in payload.items():
            if key in {"content", "path"}:
                raise ValueError("RAG_PAYLOAD_MUST_NOT_CONTAIN_PHYSICAL_CONTENT_OR_PATH")
            if isinstance(value, dict):
                self._validate_payload(value)

    def upsert(
        self,
        resource_id: str,
        tool_id: str,
        points: list[dict[str, Any]],
    ) -> None:
        for point in points:
            payload = point.get("payload") if isinstance(point, dict) else None
            if isinstance(payload, dict):
                self._validate_payload(payload)
        self.store.replace_document(resource_id, tool_id, points)


__all__ = ["QdrantHit", "RagAuthorizationBridge", "RagIndexCoordinator"]
