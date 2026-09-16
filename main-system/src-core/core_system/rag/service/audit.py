"""RAG Audit — records *what happened*, never a copy of the data.

Captured: request_id, actor, operation, scopes, architectures,
canonical/degraded, policy/generation versions, result, duration.

Never captured: full text, complete prompts, secrets — the audit is
an event ledger, not a second database.  Query text is stored only
as a bounded preview + hash.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

_PREVIEW_LEN = 120


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RagAuditRecord:
    request_id: str
    actor_id: str
    operation: str                    # query/ingest/delete/status/reconcile/admin:*
    module_scope: tuple[str, ...]
    resource_scope: tuple[str, ...] = ()
    rag_architectures: tuple[str, ...] = ()
    canonical: bool = True
    degraded_used: bool = False
    policy_version: str = ""
    generation_id: str = ""
    result: str = ""                  # ok | denied | error | degraded
    duration_ms: int = 0
    query_preview: str = ""           # <=120 chars, no full text
    query_hash: str = ""
    detail: dict[str, str] = field(default_factory=dict)
    recorded_at: float = 0.0


def make_audit_record(
    *,
    request_id: str,
    actor_id: str,
    operation: str,
    module_scope: tuple[str, ...],
    query: str = "",
    **kw: object,
) -> RagAuditRecord:
    return RagAuditRecord(
        request_id=request_id,
        actor_id=actor_id,
        operation=operation,
        module_scope=module_scope,
        query_preview=query[:_PREVIEW_LEN],
        query_hash=_digest(query) if query else "",
        recorded_at=time.time(),
        **kw,  # type: ignore[arg-type]
    )


__all__ = ["RagAuditRecord", "make_audit_record"]
