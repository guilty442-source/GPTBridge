"""Vector-store model surface — single import point for the RAG package.

With the Rust vectord engine as target-primary (codex A610), the Python
``qdrant_client`` package is needed only while the ``qdrant`` backend stays
selectable for the bounded migration window — the canonical Rust path must
import cleanly even when the package is absent or broken.

This module therefore tries the real ``qdrant_client`` models first and
falls back to attribute-container equivalents that carry exactly the
fields this codebase constructs (``id``/``vector``/``payload``,
``key``/``match``, ``must``/``should``/``must_not``, alias operations).
The vectord client only reads those attributes, so behaviour on the Rust
path is identical either way; the ``qdrant`` backend fails closed when
``QDRANT_CLIENT_AVAILABLE`` is false.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - depends on environment
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import (
        CreateAlias,
        CreateAliasOperation,
        DeleteAlias,
        DeleteAliasOperation,
        Distance,
        FieldCondition,
        Filter,
        MatchAny,
        MatchValue,
        PayloadSchemaType,
        PointStruct,
        VectorParams,
    )

    QDRANT_CLIENT_AVAILABLE = True
except Exception:  # qdrant-client absent or broken — rust path needs none
    QdrantClient = None  # type: ignore[assignment]
    QDRANT_CLIENT_AVAILABLE = False

    class _Model:
        """Minimal attribute container mirroring the qdrant model fields."""

        _FIELDS: tuple[str, ...] = ()

        def __init__(self, **kwargs: Any) -> None:
            for name in self._FIELDS:
                setattr(self, name, kwargs.pop(name, None))
            for name, value in kwargs.items():
                setattr(self, name, value)

    class Distance:  # noqa: D101 - enum stand-in
        COSINE = "Cosine"
        EUCLID = "Euclid"
        DOT = "Dot"

    class VectorParams(_Model):
        _FIELDS = ("size", "distance")

    class PointStruct(_Model):
        _FIELDS = ("id", "vector", "payload")

    class MatchValue(_Model):
        _FIELDS = ("value",)

    class MatchAny(_Model):
        _FIELDS = ("any",)

    class FieldCondition(_Model):
        _FIELDS = ("key", "match")

    class Filter(_Model):
        _FIELDS = ("must", "should", "must_not")

    class CreateAlias(_Model):
        _FIELDS = ("collection_name", "alias_name")

    class DeleteAlias(_Model):
        _FIELDS = ("alias_name",)

    class CreateAliasOperation(_Model):
        _FIELDS = ("create_alias",)

    class DeleteAliasOperation(_Model):
        _FIELDS = ("delete_alias",)

    class PayloadSchemaType:  # noqa: D101 - enum stand-in
        KEYWORD = "keyword"
        INTEGER = "integer"
        TEXT = "text"


def require_qdrant_client() -> Any:
    """Return the real ``QdrantClient`` class or fail closed.

    Only the transitional ``qdrant`` backend may call this; the canonical
    Rust path never reaches it.
    """
    if not QDRANT_CLIENT_AVAILABLE:
        raise RuntimeError(
            "QDRANT_CLIENT_UNAVAILABLE: qdrant backend selected but the "
            "qdrant_client package is not importable — the canonical path "
            "is the Rust vectord engine (VECTOR_BACKEND=rust)"
        )
    return QdrantClient


__all__ = [
    "QDRANT_CLIENT_AVAILABLE",
    "QdrantClient",
    "CreateAlias",
    "CreateAliasOperation",
    "DeleteAlias",
    "DeleteAliasOperation",
    "Distance",
    "FieldCondition",
    "Filter",
    "MatchAny",
    "MatchValue",
    "PayloadSchemaType",
    "PointStruct",
    "VectorParams",
    "require_qdrant_client",
]
