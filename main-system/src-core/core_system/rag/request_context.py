"""RAG Request Context & Policy — 端到端追蹤與生命週期管理。

Request ID 貫穿：IPC → Router → Embedding → Qdrant → PG → RRF → Rerank → LLM → Citation
Policy Object 固定檢索策略版本
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from .retrieval_pipeline import RagPolicy, DEFAULT_POLICY

_logger = logging.getLogger("gptbridge.rag.request")


# Context variable for request ID propagation
rag_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("rag_request_id", default="")
rag_policy_var: contextvars.ContextVar[RagPolicy] = contextvars.ContextVar("rag_policy", default=DEFAULT_POLICY)


class LifecycleState(str, Enum):
    """Complete resource lifecycle states."""
    SOURCE = "SOURCE"                    # Raw input received
    PARSED = "PARSED"                    # Parsed/extracted
    CHUNKED = "CHUNKED"                  # Chunked with stable IDs
    EMBEDDED = "EMBEDDED"                # Embedded (cache or computed)
    INDEX_PENDING = "INDEX_PENDING"      # Queued for indexing
    CANONICAL_INDEXED = "CANONICAL_INDEXED"  # In Qdrant + PG
    ACTIVE = "ACTIVE"                    # Serving queries (alias points here)
    STALE = "STALE"                      # Source updated, needs refresh
    TOMBSTONED = "TOMBSTONED"            # Deleted, awaiting cleanup
    DEGRADED_PENDING = "DEGRADED_PENDING" # Canonical down, in local store
    RECONCILING = "RECONCILING"          # Mismatch detected, fixing


@dataclass(frozen=True)
class RagRequestContext:
    """End-to-end request context for tracing."""
    request_id: str
    policy: RagPolicy
    query: str
    module_id: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    trace: dict[str, Any] = field(default_factory=dict)

    def add_trace(self, stage: str, data: dict[str, Any]) -> None:
        self.trace[stage] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }


class RagRequestManager:
    """Manages request context and policy for a RAG request."""

    def __init__(self, policy: Optional[RagPolicy] = None) -> None:
        self.policy = policy or DEFAULT_POLICY

    def start_request(
        self,
        query: str,
        module_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> RagRequestContext:
        """Start a new RAG request with fresh request ID."""
        request_id = f"rag-{uuid.uuid4().hex[:12]}"
        context = RagRequestContext(
            request_id=request_id,
            policy=self.policy,
            query=query,
            module_id=module_id,
            user_id=user_id,
            session_id=session_id,
        )

        # Set context variables for propagation
        rag_request_id_var.set(request_id)
        rag_policy_var.set(self.policy)

        _logger.info("RAG Request %s started: query='%s...' module=%s",
                     request_id, query[:50], module_id)
        return context

    def end_request(self, context: RagRequestContext, result: dict[str, Any]) -> None:
        """End request and log summary."""
        duration_ms = 0
        try:
            start = datetime.fromisoformat(context.started_at.replace("Z", "+00:00"))
            duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        except Exception:
            pass

        _logger.info(
            "RAG Request %s completed: duration=%dms hits=%d state=%s",
            context.request_id,
            duration_ms,
            result.get("hit_count", 0),
            result.get("state", "unknown"),
        )

        # Clear context variables
        rag_request_id_var.set("")
        rag_policy_var.set(DEFAULT_POLICY)


def get_current_request_id() -> str:
    """Get current request ID from context."""
    return rag_request_id_var.get() or ""


def get_current_policy() -> RagPolicy:
    """Get current policy from context."""
    return rag_policy_var.get()


def trace_stage(context: RagRequestContext, stage: str, **data: Any) -> None:
    """Add trace entry for a pipeline stage."""
    context.add_trace(stage, data)


# Lifecycle transition helpers
VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.SOURCE: {LifecycleState.PARSED, LifecycleState.TOMBSTONED},
    LifecycleState.PARSED: {LifecycleState.CHUNKED, LifecycleState.STALE},
    LifecycleState.CHUNKED: {LifecycleState.EMBEDDED, LifecycleState.STALE},
    LifecycleState.EMBEDDED: {LifecycleState.INDEX_PENDING, LifecycleState.STALE},
    LifecycleState.INDEX_PENDING: {LifecycleState.CANONICAL_INDEXED, LifecycleState.DEGRADED_PENDING, LifecycleState.STALE},
    LifecycleState.CANONICAL_INDEXED: {LifecycleState.ACTIVE, LifecycleState.STALE, LifecycleState.TOMBSTONED},
    LifecycleState.ACTIVE: {LifecycleState.STALE, LifecycleState.TOMBSTONED, LifecycleState.RECONCILING},
    LifecycleState.STALE: {LifecycleState.SOURCE, LifecycleState.RECONCILING},
    LifecycleState.TOMBSTONED: set(),  # Terminal
    LifecycleState.DEGRADED_PENDING: {LifecycleState.CANONICAL_INDEXED, LifecycleState.STALE},
    LifecycleState.RECONCILING: {LifecycleState.ACTIVE, LifecycleState.STALE, LifecycleState.DEGRADED_PENDING},
}


def can_transition(from_state: LifecycleState, to_state: LifecycleState) -> bool:
    """Check if lifecycle transition is valid."""
    return to_state in VALID_TRANSITIONS.get(from_state, set())


def transition_resource(
    version_controller: Any,  # VersionController
    resource_id: str,
    module_id: str,
    from_state: LifecycleState,
    to_state: LifecycleState,
    generation_id: str,
    **kwargs: Any,
) -> tuple[bool, str]:
    """Attempt lifecycle transition with version control.

    Returns (success, message).
    """
    if not can_transition(from_state, to_state):
        return False, f"Invalid transition: {from_state} -> {to_state}"

    current = version_controller.get_current_version(module_id, resource_id)
    if not current:
        return False, f"Resource not found: {module_id}/{resource_id}"

    if current.state != from_state:
        return False, f"State mismatch: expected {from_state}, got {current.state}"

    # Update version with new state
    success, new_version = version_controller.try_update_version(
        module_id=module_id,
        resource_id=resource_id,
        expected_version=current.version,
        new_content_hash=current.content_hash,
        new_generation_id=generation_id,
        knowledge_kind=current.knowledge_kind,
        new_state=to_state,
        chunk_policy_version=current.chunk_policy_version,
        chunk_count=current.chunk_count,
        embedding_model=current.embedding_model,
        embedding_dimension=current.embedding_dimension,
    )

    if success:
        return True, f"Transitioned to {to_state} (version {new_version.version})"
    return False, "Version conflict: concurrent update detected"


class LifecycleManager:
    """Manages resource lifecycle transitions."""

    def __init__(self, version_controller: Any) -> None:
        self.version_controller = version_controller

    def transition(
        self,
        resource_id: str,
        module_id: str,
        to_state: LifecycleState,
        generation_id: str,
    ) -> tuple[bool, str]:
        """Transition resource to new state."""
        current = self.version_controller.get_current_version(module_id, resource_id)
        if not current:
            return False, f"Resource not found: {module_id}/{resource_id}"

        return transition_resource(
            self.version_controller,
            resource_id,
            module_id,
            LifecycleState(current.state),
            to_state,
            generation_id,
        )

    def mark_stale(self, resource_id: str, module_id: str, generation_id: str) -> tuple[bool, str]:
        """Mark resource as STALE (source has newer version)."""
        return self.transition(resource_id, module_id, LifecycleState.STALE, generation_id)

    def mark_tombstoned(self, resource_id: str, module_id: str, generation_id: str) -> tuple[bool, str]:
        """Mark resource as TOMBSTONED (deleted)."""
        return self.transition(resource_id, module_id, LifecycleState.TOMBSTONED, generation_id)

    def mark_active(self, resource_id: str, module_id: str, generation_id: str) -> tuple[bool, str]:
        """Mark resource as ACTIVE (canonical indexed)."""
        return self.transition(resource_id, module_id, LifecycleState.ACTIVE, generation_id)

    def mark_reconciling(self, resource_id: str, module_id: str, generation_id: str) -> tuple[bool, str]:
        """Mark resource as RECONCILING (mismatch detected)."""
        return self.transition(resource_id, module_id, LifecycleState.RECONCILING, generation_id)


class PolicyRegistry:
    """Registry of RAG policies by ID."""

    def __init__(self) -> None:
        self._policies: dict[str, RagPolicy] = {"default": DEFAULT_POLICY}

    def register(self, policy: RagPolicy) -> None:
        self._policies[policy.policy_id] = policy

    def get(self, policy_id: str) -> RagPolicy:
        return self._policies.get(policy_id, DEFAULT_POLICY)

    def list_policies(self) -> list[RagPolicy]:
        return list(self._policies.values())


POLICY_REGISTRY = PolicyRegistry()


__all__ = [
    "RagRequestContext",
    "RagRequestManager",
    "RagPolicy",
    "DEFAULT_POLICY",
    "LifecycleState",
    "VALID_TRANSITIONS",
    "can_transition",
    "transition_resource",
    "LifecycleManager",
    "PolicyRegistry",
    "POLICY_REGISTRY",
    "get_current_request_id",
    "get_current_policy",
    "trace_stage",
    "rag_request_id_var",
    "rag_policy_var",
]