"""RAG Commands — the Information Channel carries *intent* only.

Callers say what they want to find and within which authorized scope;
they may never name physical storage:

    FORBIDDEN on a command: collection names, raw qdrant filter JSON,
    raw SQL, physical paths, point IDs.

Every storage decision stays inside the RAG service (Canonical
Gateway owns PG/Qdrant translation).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..orchestration.evidence import RagArchitecture

# Field names that would leak storage decisions to the caller.
_FORBIDDEN_KEYS = frozenset({
    "collection", "collection_name", "qdrant_filter", "filter",
    "sql", "query_sql", "point_id", "point_ids", "scroll",
    "path", "file_path", "physical_path", "payload",
})
_SQLISH = re.compile(r"\b(?:select|insert|update|delete|drop|where)\b", re.I)


class CommandRejected(ValueError):
    """Caller tried to specify storage-level detail."""


def _validate_scope(scope: dict[str, Any]) -> None:
    bad = _FORBIDDEN_KEYS & {str(k).lower() for k in scope}
    if bad:
        raise CommandRejected(
            f"scope may not contain storage-level keys: {sorted(bad)}"
        )
    for v in scope.values():
        if isinstance(v, str) and _SQLISH.search(v):
            raise CommandRejected("scope values may not contain SQL")


@dataclass(frozen=True, slots=True)
class RagQueryCommand:
    """Intent-only query command."""

    request_id: str
    actor_id: str
    module_id: str
    query: str
    rag_architectures: tuple[RagArchitecture, ...] = ()
    generation_mode: str = "general"
    scope: dict[str, Any] = field(default_factory=dict)
    task_instruction: str = ""
    session_id: str = ""

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise CommandRejected("query may not be empty")
        _validate_scope(self.scope)


@dataclass(frozen=True, slots=True)
class RagIngestCommand:
    """Ingest intent — caller supplies a resource identity and the
    owning module's locator, never a storage target."""

    request_id: str
    actor_id: str
    module_id: str
    resource_id: str
    locator_id: str
    resource_type: str = "document"
    data_category: str = "internal"
    scope: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_scope(self.scope)


@dataclass(frozen=True, slots=True)
class RagDeleteCommand:
    request_id: str
    actor_id: str
    module_id: str
    resource_id: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RagReconcileCommand:
    request_id: str
    actor_id: str
    module_id: str = ""          # empty = all modules
    scope: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_scope(self.scope)


@dataclass(frozen=True, slots=True)
class RagStatusCommand:
    request_id: str
    actor_id: str


__all__ = [
    "CommandRejected",
    "RagDeleteCommand",
    "RagIngestCommand",
    "RagQueryCommand",
    "RagReconcileCommand",
    "RagStatusCommand",
]
