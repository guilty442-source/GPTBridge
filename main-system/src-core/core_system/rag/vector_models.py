"""Vector-store model surface — single import point for the RAG package.

The canonical vector engine is the Rust ``vectord`` service (codex A611).
These attribute-container models carry exactly the fields this codebase
constructs (``id``/``vector``/``payload``, ``key``/``match``,
``must``/``should``/``must_not``, alias operations).  The vectord client
only reads those attributes, so the transport-neutral shapes are
sufficient on their own.
"""

from __future__ import annotations

from typing import Any


class _Model:
    """Minimal attribute container for vector-engine payloads."""

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


__all__ = [
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
]
