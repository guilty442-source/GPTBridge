"""RAG four sub-architecture adapters — A549 RAG plane wiring.

A549 keeps the canonical RAG plane as Hybrid, Code, Memory and Agentic
sub-architectures.  This module registers the four adapters, bridges the
legacy retriever signature ``(query, scope) -> [evidence]`` into the DAG
retrieval node contract, and fails closed when a requested
sub-architecture is unavailable or its agentic round budget is unbounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .dag import (
    HARD_MAX_ROUNDS,
    RagDagExecutionContext,
    RagDagNode,
    RagDagNodeType,
)
from .rag_contracts import RagType

SUBARCHITECTURES: tuple[str, ...] = ("hybrid", "code", "memory", "agentic")

LEGACY_RETRIEVER_KEYS: Mapping[str, str] = {
    "hybrid": "retrieve_hybrid",
    "code": "retrieve_code",
    "memory": "retrieve_memory",
    "agentic": "retrieve_agentic",
}


class SubArchitectureUnavailable(RuntimeError):
    """Raised when a sub-architecture has no registered adapter."""

    failure_code = "SUBARCHITECTURE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SubArchitectureAdapter:
    """One sub-architecture's bounded retrieval callable."""

    name: str
    retriever: Callable[[str, Mapping[str, Any]], Sequence[Any]]
    bounded_rounds: int = 1

    def invoke(
        self,
        query: str,
        *,
        module_ids: tuple[str, ...],
        permission_scope: str,
    ) -> tuple[Any, ...]:
        scope = {
            "module_ids": list(module_ids),
            "permission_scope": permission_scope,
        }
        return tuple(self.retriever(query, scope))


class SubArchitectureRegistry:
    """Registered four-sub-architecture adapter set."""

    def __init__(self, adapters: Mapping[str, SubArchitectureAdapter]) -> None:
        self._adapters = dict(adapters)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def require(self, name: str) -> SubArchitectureAdapter:
        adapter = self._adapters.get(str(name or "").strip().casefold())
        if adapter is None:
            raise SubArchitectureUnavailable(f"sub-architecture-unavailable:{name}")
        if not 1 <= adapter.bounded_rounds <= HARD_MAX_ROUNDS:
            raise SubArchitectureUnavailable(
                f"sub-architecture-rounds-out-of-envelope:{name}"
            )
        return adapter

    @classmethod
    def from_retrievers(
        cls,
        retrievers: Mapping[str, Callable[[str, Mapping[str, Any]], Sequence[Any]]],
        *,
        agentic_rounds: int = 1,
    ) -> "SubArchitectureRegistry":
        adapters: dict[str, SubArchitectureAdapter] = {}
        for name, key in LEGACY_RETRIEVER_KEYS.items():
            retriever = retrievers.get(key)
            if retriever is None:
                continue
            adapters[name] = SubArchitectureAdapter(
                name=name,
                retriever=retriever,
                bounded_rounds=agentic_rounds if name == "agentic" else 1,
            )
        return cls(adapters)


def build_retrieval_handler(registry: SubArchitectureRegistry) -> Callable[..., Mapping[str, Any]]:
    """Build the DAG retrieval handler that dispatches on ``rag_type``."""

    def handler(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        rag_type = str(node.inputs.get("rag_type") or "hybrid").strip().casefold()
        adapter = registry.require(rag_type)
        candidates = adapter.invoke(
            str(node.inputs.get("query") or ""),
            module_ids=context.module_ids,
            permission_scope=context.permission_scope,
        )
        return {
            "ok": True,
            "evidence": {
                "candidates": list(candidates),
                "provenance": {
                    "rag_type": rag_type,
                    "candidate_count": len(candidates),
                    "bounded_rounds": adapter.bounded_rounds,
                },
            },
        }

    return handler


def assert_registered_subarchitectures() -> None:
    """Fail closed unless the adapter set covers the declared RagType set."""
    declared = {rag_type.value for rag_type in RagType}
    if set(SUBARCHITECTURES) != declared:
        raise SubArchitectureUnavailable(
            "sub-architecture-set-mismatch:"
            f"{sorted(set(SUBARCHITECTURES) ^ declared)}"
        )


__all__ = [
    "LEGACY_RETRIEVER_KEYS",
    "SUBARCHITECTURES",
    "SubArchitectureAdapter",
    "SubArchitectureRegistry",
    "SubArchitectureUnavailable",
    "assert_registered_subarchitectures",
    "build_retrieval_handler",
]
