"""RagApplicationService — the single governed entry point.

Every adapter (CLI, IPC, UI, agent) calls this service; none owns
RAG logic.  Five public entry points only:

    query()  ingest()  delete()  status()  reconcile()

Flow for query():
    RagQueryCommand (intent only)
      -> capability check (rag.query)
      -> Permission Sovereign point 1: query admission
      -> orchestrator plan/retrieve/fuse/rerank (Canonical Gateway
         owns all PG/Qdrant translation — no other module touches
         the vector store)
      -> Permission Sovereign point 2: per-resource authorization
      -> evidence pool -> context -> generation decision
      -> RagResponse (+ audit record, no data copy)

Qdrant is reachable only inside this service's canonical gateway —
there is no API surface that takes a collection name or raw filter.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..orchestration.evidence import (
    Citation,
    RagEvidence,
    SourceAuthority,
    authority_of,
    make_citation,
)
from ..orchestration.orchestrator import OrchestratorResult, RagOrchestrator
from ..orchestration.sufficiency import SufficiencyVerdict
from .audit import RagAuditRecord, make_audit_record
from .authorization import PermissionSovereign
from .capabilities import RagCapability, require_capability
from .commands import (
    RagDeleteCommand,
    RagIngestCommand,
    RagQueryCommand,
    RagReconcileCommand,
    RagStatusCommand,
)
from .response import (
    GenerationMetadata,
    HealthMetadata,
    PolicyMetadata,
    RagResponse,
    RetrievalMetadata,
)

AuditSink = Callable[[RagAuditRecord], None]
ResourceAuthorizer = Callable[[RagEvidence, str], bool]


@dataclass(frozen=True, slots=True)
class ServiceResult:
    response: RagResponse
    audit: RagAuditRecord


class RagApplicationService:
    """Application facade — adapters call only this."""

    def __init__(
        self,
        orchestrator: RagOrchestrator,
        sovereign: PermissionSovereign,
        *,
        actor_role_of: Callable[[str], str] | None = None,
        audit_sink: Optional[AuditSink] = None,
        policy_version: str = "",
    ) -> None:
        self._orchestrator = orchestrator
        self._sovereign = sovereign
        self._role_of = actor_role_of or (lambda actor: "agent")
        self._audit_sink = audit_sink or (lambda rec: None)
        self._policy_version = policy_version

    # ------------------------------------------------------------------
    # query()
    # ------------------------------------------------------------------
    def query(self, command: RagQueryCommand) -> ServiceResult:
        started = time.monotonic()
        role = self._role_of(command.actor_id)
        module_scope = tuple(
            command.scope.get("module_ids", (command.module_id,))
        )

        def _audit(result: str, **kw: Any) -> RagAuditRecord:
            rec = make_audit_record(
                request_id=command.request_id,
                actor_id=command.actor_id,
                operation="rag.query",
                module_scope=module_scope,
                query=command.query,
                result=result,
                duration_ms=int((time.monotonic() - started) * 1000),
                **kw,
            )
            self._audit_sink(rec)
            return rec

        def _deny(reason: str) -> ServiceResult:
            return ServiceResult(
                response=RagResponse(
                    request_id=command.request_id,
                    error=reason,
                    policy=PolicyMetadata(
                        policy_version=self._policy_version,
                        admission="denied",
                    ),
                ),
                audit=_audit("denied"),
            )

        try:
            require_capability(role, RagCapability.QUERY)
        except PermissionError as exc:
            return _deny(str(exc))

        # Point 1 — query admission
        admission = self._sovereign.admit_query(
            command.actor_id, module_scope, "rag.query"
        )
        if not admission.allowed:
            return _deny(f"admission:{admission.reason}")

        # Retrieval — scope carries only the admitted module ids
        scope = dict(command.scope)
        scope["module_ids"] = admission.module_scope
        result = self._orchestrator.query(
            command.query,
            generation_mode=command.generation_mode,
            module_ids=admission.module_scope,
            session_id=command.session_id,
            explicit_architectures=command.rag_architectures or None,
            task_instruction=command.task_instruction,
            plan_id=command.request_id,
        )

        # Point 2 — per-resource authorization
        granted: list[RagEvidence] = []
        denies = 0
        for e in result.evidence:
            grant = self._sovereign.authorize_resource(
                command.actor_id, e.resource_id, e.module_id,
                str(e.provenance.get("data_category", "internal")),
            )
            if grant.allowed:
                granted.append(e)
            else:
                denies += 1

        citations = tuple(
            make_citation(i, e) for i, e in enumerate(granted, start=1)
        )
        degraded = result.degraded or any(
            authority_of(e) is SourceAuthority.DEGRADED_CACHE for e in granted
        )
        resp = RagResponse(
            request_id=command.request_id,
            answer="",                      # filled by generation stage
            evidence=tuple(granted),
            citations=citations,
            retrieval=RetrievalMetadata(
                architectures=tuple(
                    a.value for a in result.plan.rag_architectures
                ),
                canonical=not degraded,
                degraded_used=degraded,
                candidate_count=len(result.evidence),
                reranked_count=len(result.evidence),
                selected_count=len(granted),
                rounds=max(1, result.agentic_rounds or 1),
                sufficiency=result.report.verdict.value,
            ),
            generation=GenerationMetadata(
                mode=result.generation.mode.value,
                model_hint=result.generation.model_hint,
            ),
            policy=PolicyMetadata(
                policy_version=self._policy_version,
                admission="allowed",
                resource_grants=len(granted),
                resource_denies=denies,
            ),
            health=HealthMetadata(
                backend_state="degraded" if degraded else "canonical",
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
        )
        return ServiceResult(
            response=resp,
            audit=_audit(
                "degraded" if degraded else "ok",
                rag_architectures=tuple(
                    a.value for a in result.plan.rag_architectures
                ),
                degraded_used=degraded,
                generation_id=result.plan.plan_id,
            ),
        )

    # ------------------------------------------------------------------
    # ingest() / delete() / status() / reconcile()
    # ------------------------------------------------------------------
    def ingest(self, command: RagIngestCommand) -> RagAuditRecord:
        role = self._role_of(command.actor_id)
        require_capability(role, RagCapability.INGEST)
        admission = self._sovereign.admit_query(
            command.actor_id, (command.module_id,), "rag.ingest"
        )
        if not admission.allowed:
            raise PermissionError(f"ingest denied: {admission.reason}")
        rec = make_audit_record(
            request_id=command.request_id,
            actor_id=command.actor_id,
            operation="rag.ingest",
            module_scope=(command.module_id,),
            resource_scope=(command.resource_id,),
            result="accepted",
        )
        self._audit_sink(rec)
        return rec

    def delete(self, command: RagDeleteCommand) -> RagAuditRecord:
        role = self._role_of(command.actor_id)
        require_capability(role, RagCapability.DELETE)
        rec = make_audit_record(
            request_id=command.request_id,
            actor_id=command.actor_id,
            operation="rag.delete",
            module_scope=(command.module_id,),
            resource_scope=(command.resource_id,),
            result="accepted",
        )
        self._audit_sink(rec)
        return rec

    def status(self, command: RagStatusCommand) -> RagAuditRecord:
        role = self._role_of(command.actor_id)
        require_capability(role, RagCapability.STATUS)
        rec = make_audit_record(
            request_id=command.request_id,
            actor_id=command.actor_id,
            operation="rag.status",
            module_scope=(),
            result="ok",
        )
        self._audit_sink(rec)
        return rec

    def reconcile(self, command: RagReconcileCommand) -> RagAuditRecord:
        role = self._role_of(command.actor_id)
        require_capability(role, RagCapability.RECONCILE)
        rec = make_audit_record(
            request_id=command.request_id,
            actor_id=command.actor_id,
            operation="rag.reconcile",
            module_scope=(command.module_id,) if command.module_id else (),
            result="accepted",
        )
        self._audit_sink(rec)
        return rec


__all__ = ["RagApplicationService", "ServiceResult"]
