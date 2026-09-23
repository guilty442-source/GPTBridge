"""Production node handlers for the RETRIEVAL_CHAIN DAG kind (A549).

The benchmark wires stub handlers; these are the real ones — each node
type delegates to the same governed primitives the orchestrator uses,
so a DAG run cannot diverge from the canonical retrieval path:

    RETRIEVAL      -> RagOrchestrator.dispatch (bounded parallel lanes)
    FUSION         -> architecture_fusion + mark_conflicts
    RERANK         -> the orchestrator's governed reranker (fail-open
                      to fused order when no reranker is configured)
    CONTEXT_BUILD  -> build_context

Every handler returns the catalog-declared evidence keys; node inputs
reference upstream evidence via ``"<node_id>.<key>"`` strings resolved
here — nothing reaches around the executor's upstream map.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..orchestration.context_builder import build_context
from ..orchestration.evidence import RagArchitecture, RagEvidence
from ..orchestration.fusion import architecture_fusion, mark_conflicts
from .contracts import (
    RagDagExecutionContext,
    RagDagNode,
    RagDagNodeType,
)

_ARCH_BY_NAME = {arch.value: arch for arch in RagArchitecture}
# tolerate bare names ("hybrid") alongside enum values ("hybrid-rag")
_ARCH_BY_NAME.update(
    {arch.value.removesuffix("-rag"): arch for arch in RagArchitecture}
)


def _resolve(ref: Any, upstream: Mapping[str, Mapping[str, Any]]) -> Any:
    """Resolve a ``"<node_id>.<key>"`` evidence reference; anything else
    is returned as-is (literals pass through)."""
    if isinstance(ref, str) and "." in ref:
        node_id, _, key = ref.partition(".")
        return upstream.get(node_id, {}).get(key)
    return ref


def _resolve_many(refs: Any, upstream: Mapping[str, Mapping[str, Any]]) -> list[Any]:
    values = refs if isinstance(refs, (list, tuple)) else [refs]
    out: list[Any] = []
    for ref in values:
        resolved = _resolve(ref, upstream)
        if isinstance(resolved, (list, tuple)):
            out.extend(resolved)
        elif resolved is not None:
            out.append(resolved)
    return out


def retrieval_chain_handlers(
    orchestrator: Any,
    *,
    query: str,
    scope: Mapping[str, Any],
    task_instruction: str = "",
) -> dict[RagDagNodeType, Any]:
    """Build the per-query handler set for a RETRIEVAL_CHAIN run.

    ``query``/``scope``/``task_instruction`` are bound at build time —
    handlers see only node inputs and upstream evidence, matching the
    executor's NodeHandler contract.
    """
    reranker = getattr(orchestrator, "reranker", None)

    def _retrieval(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        names = node.inputs.get("rag_types") or ("hybrid",)
        archs = tuple(
            arch
            for name in names
            if (arch := _ARCH_BY_NAME.get(str(name))) is not None
        ) or (RagArchitecture.HYBRID,)
        call_scope = dict(scope)
        call_scope["module_ids"] = tuple(context.module_ids)
        pools = orchestrator.dispatch(archs, query, call_scope)
        candidates: list[RagEvidence] = [
            evidence for pool in pools.values() for evidence in pool
        ]
        return {
            "ok": True,
            "evidence": {
                "candidates": candidates,
                "provenance": {
                    "architectures": [a.value for a in pools],
                    "module_ids": list(context.module_ids),
                },
            },
        }

    def _fusion(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        candidates = _resolve_many(node.inputs.get("candidate_sets"), upstream)
        pools: dict[RagArchitecture, list[RagEvidence]] = {}
        for evidence in candidates:
            if isinstance(evidence, RagEvidence):
                pools.setdefault(evidence.rag_type, []).append(evidence)
        fused = mark_conflicts(architecture_fusion(pools))
        return {
            "ok": True,
            "evidence": {
                "fused_candidates": list(fused),
                "fusion_method": "architecture_fusion",
            },
        }

    def _rerank(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        fused = list(_resolve(node.inputs.get("fused_candidates"), upstream) or [])
        if reranker is not None and fused:
            ranked = list(reranker(query, fused))
            model = getattr(reranker, "model_name", "orchestrator-reranker")
        else:
            ranked, model = fused, "none"
        return {
            "ok": True,
            "evidence": {
                "reranked_candidates": ranked,
                "reranker_model": model,
            },
        }

    def _context_build(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        ranked = list(
            _resolve(node.inputs.get("reranked_candidates"), upstream) or []
        )
        built = build_context(
            ranked,
            system_governance="",
            task_instruction=task_instruction or query,
        )
        return {
            "ok": True,
            "evidence": {
                "context_text": built.text,
                "token_budget": int(
                    node.inputs.get("max_context_tokens") or 0
                ),
                "built_context": built,
                "citations": list(built.citations),
            },
        }

    return {
        RagDagNodeType.RETRIEVAL: _retrieval,
        RagDagNodeType.FUSION: _fusion,
        RagDagNodeType.RERANK: _rerank,
        RagDagNodeType.CONTEXT_BUILD: _context_build,
    }


# ----------------------------------------------------------------------
# QUERY / MULTI_RAG kinds — cache gate + inference + citation validation
# ----------------------------------------------------------------------

# CacheGateDecision.checks key -> declared CACHE_VALIDATE evidence key
_GATE_CHECK_KEYS: Mapping[str, str] = {
    "scope": "scope_check",
    "permission": "permission_check",
    "revision": "revision_check",
    "expiry": "expiry_check",
    "authority": "authority_check",
}

_CITATION_LABEL = re.compile(r"\[([A-Z]+\d+)\]")


def query_chain_handlers(
    orchestrator: Any,
    *,
    query: str,
    scope: Mapping[str, Any],
    task_instruction: str = "",
    cache_store: Any = None,
    cache_request: Any = None,
    generate: Any = None,
    policy_version: str = "",
) -> dict[RagDagNodeType, Any]:
    """Production handler set for QUERY / MULTI_RAG plans.

    Extends the retrieval chain with the gated cache lookup/validate pair
    (``CagCacheStore.get`` is the real gate — the same call the service
    facade makes) plus MODEL_INFERENCE and CITATION_VALIDATION.

    ``generate`` is the governed inference callable
    ``(context_text) -> mapping`` injected by the composition layer
    (e.g. bridged to the native model runtime); when absent the node
    fails closed — an answer is never fabricated.
    """
    handlers = retrieval_chain_handlers(
        orchestrator,
        query=query,
        scope=scope,
        task_instruction=task_instruction,
    )

    def _cache_lookup(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        if cache_store is None or cache_request is None:
            return {"ok": False, "evidence": {"error": "cache-lookup:no-store"}}
        entry, decision = cache_store.get(cache_request)
        return {
            "ok": True,
            "evidence": {
                "cache_hit": bool(entry is not None and decision.allowed),
                "cache_metadata": decision.to_record(),
                "cache_entry": entry,
                "cache_decision": decision,
            },
        }

    def _cache_validate(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        decision = _resolve("cache-lookup.cache_decision", upstream)
        checks = getattr(decision, "checks", None) or {}
        evidence = {
            evidence_key: bool(checks.get(gate_key, False))
            for gate_key, evidence_key in _GATE_CHECK_KEYS.items()
        }
        evidence["cache_verdict"] = (
            "allowed"
            if getattr(decision, "allowed", False)
            else f"denied:{getattr(decision, 'reason', 'no-decision')}"
        )
        # A miss or a denied entry is a validated negative — the chain
        # proceeds to retrieval; it is never an execution error.
        return {"ok": True, "evidence": evidence}

    def _model_inference(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        if generate is None:
            return {"ok": False, "evidence": {"error": "model-inference:no-generate-callable"}}
        context_text = _resolve(node.inputs.get("context_text"), upstream) or ""
        outcome = generate(str(context_text))
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            error = "model-inference:failed"
            if isinstance(outcome, Mapping):
                error = f"model-inference:{outcome.get('error_code') or 'failed'}"
            return {"ok": False, "evidence": {"error": error}}
        answer_text = str(
            outcome.get("answer_text") or outcome.get("text") or ""
        )
        if not answer_text.strip():
            return {"ok": False, "evidence": {"error": "model-inference:empty-answer"}}
        return {
            "ok": True,
            "evidence": {
                "answer_text": answer_text,
                "model_identity": str(
                    outcome.get("model_id")
                    or outcome.get("model_identity")
                    or node.inputs.get("model_identity")
                    or ""
                ),
                "policy_version": str(
                    outcome.get("policy_version") or policy_version
                ),
            },
        }

    def _citation_validation(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        answer_text = str(_resolve(node.inputs.get("answer_text"), upstream) or "")
        citations = [
            citation
            for evidence_map in upstream.values()
            for citation in (evidence_map.get("citations") or ())
        ]
        candidates_by_id: dict[str, Any] = {}
        for candidates_key in (
            "reranked_candidates",
            "fused_candidates",
            "candidates",
        ):
            for candidates_map in upstream.values():
                for candidate in (candidates_map.get(candidates_key) or ()):
                    if getattr(candidate, "authorized", False):
                        candidates_by_id[
                            getattr(candidate, "evidence_id", "")
                        ] = candidate
        validated: list[Any] = []
        rejected = 0
        for citation in citations:
            ev = candidates_by_id.get(getattr(citation, "evidence_id", ""))
            if ev is None:
                rejected += 1
                continue
            # Emit rag_contracts.Citation-compatible records (the
            # pipeline-layer citation shape the service expects).
            provenance = getattr(ev, "provenance", None) or {}
            validated.append(
                {
                    "citation_id": str(
                        getattr(citation, "label", "") or ev.evidence_id
                    ),
                    "resource_id": getattr(ev, "resource_id", ""),
                    "chunk_id": getattr(ev, "chunk_id", ""),
                    "locator_id": getattr(citation, "locator_id", "")
                    or getattr(ev, "locator_id", ""),
                    "character_start": int(
                        provenance.get("character_start", 0) or 0
                    ),
                    "character_end": int(
                        provenance.get("character_end", 0) or 0
                    ),
                    "content_hash": getattr(ev, "content_hash", ""),
                    "generation_id": getattr(ev, "generation_id", ""),
                    "retrieval_score": float(
                        getattr(ev, "reranker_score", 0.0)
                        or getattr(ev, "dense_score", 0.0)
                        or 0.0
                    ),
                    "module_id": getattr(ev, "module_id", "") or None,
                }
            )
        # Citation labels the answer actually cites must all resolve —
        # labels carry brackets ("[R1]"); normalize both sides to bare
        # tokens before comparing.
        known_labels = {
            str(getattr(citation, "label", "")).strip("[]")
            for citation in citations
        }
        unresolved = [
            label for label in _CITATION_LABEL.findall(answer_text)
            if label not in known_labels
        ]
        verdict = (
            "validated"
            if validated and not rejected and not unresolved
            else "rejected"
        )
        evidence = {
            "validated_citations": validated,
            "citation_verdict": verdict,
            "rejected_count": rejected,
            "unresolved_labels": unresolved,
        }
        if verdict != "validated":
            # Fail closed (A549: refuse uncited/unverifiable results);
            # the evidence is still recorded for audit.
            evidence["error"] = f"citation-validation:{verdict}"
            return {"ok": False, "evidence": evidence}
        return {"ok": True, "evidence": evidence}

    handlers.update(
        {
            RagDagNodeType.CACHE_LOOKUP: _cache_lookup,
            RagDagNodeType.CACHE_VALIDATE: _cache_validate,
            RagDagNodeType.MODEL_INFERENCE: _model_inference,
            RagDagNodeType.CITATION_VALIDATION: _citation_validation,
        }
    )
    return handlers


# ----------------------------------------------------------------------
# INDEX / REBUILD / REPAIR kinds — side-effecting write path
# ----------------------------------------------------------------------


def write_path_handlers(
    *,
    index_fn: Any = None,
    verify_fn: Any = None,
    publish_fn: Any = None,
    repair_fn: Any = None,
) -> dict[RagDagNodeType, Any]:
    """Production handler set for the side-effecting write-path kinds.

    Each callable is injected by the composition layer so the DAG
    executor wraps — never bypasses — the governed write primitives:

        index_fn(resource_ids, content_hash, generation_id) -> mapping
        verify_fn(verification_target) -> mapping
        publish_fn(publish_target, verification_result) -> mapping
        repair_fn(repair_target, repair_plan) -> mapping

    A missing callable fails the node closed; side-effecting kinds can
    never run on stub evidence.
    """

    def _missing(name: str):
        def handler(
            node: RagDagNode,
            context: RagDagExecutionContext,
            upstream: Mapping[str, Mapping[str, Any]],
        ) -> Mapping[str, Any]:
            return {"ok": False, "evidence": {"error": f"{name}:no-callable"}}

        return handler

    def _index(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        outcome = index_fn(
            resource_ids=tuple(node.inputs.get("resource_id") or ()),
            content_hash=node.inputs.get("content_hash") or "",
            generation_id=node.inputs.get("generation_id") or "",
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            error = "index:failed"
            if isinstance(outcome, Mapping):
                error = str(outcome.get("error") or error)
            return {"ok": False, "evidence": {**dict(outcome or {}), "error": error}}
        evidence = dict(outcome)
        evidence.pop("ok", None)
        return {"ok": True, "evidence": evidence}

    def _verification(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        outcome = verify_fn(node.inputs.get("verification_target") or "")
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            error = "verification:failed"
            if isinstance(outcome, Mapping):
                error = str(outcome.get("error") or error)
            return {"ok": False, "evidence": {**dict(outcome or {}), "error": error}}
        evidence = dict(outcome)
        evidence.pop("ok", None)
        return {"ok": True, "evidence": evidence}

    def _publish_barrier(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        verification = _resolve(node.inputs.get("verification_result"), upstream)
        outcome = publish_fn(
            node.inputs.get("publish_target") or "",
            verification,
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            error = "publish-barrier:failed"
            if isinstance(outcome, Mapping):
                error = str(outcome.get("error") or error)
            return {"ok": False, "evidence": {**dict(outcome or {}), "error": error}}
        evidence = dict(outcome)
        evidence.pop("ok", None)
        return {"ok": True, "evidence": evidence}

    def _repair(
        node: RagDagNode,
        context: RagDagExecutionContext,
        upstream: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        outcome = repair_fn(
            node.inputs.get("repair_target") or "",
            node.inputs.get("repair_plan") or "",
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            error = "repair:failed"
            if isinstance(outcome, Mapping):
                error = str(outcome.get("error") or error)
            return {"ok": False, "evidence": {**dict(outcome or {}), "error": error}}
        evidence = dict(outcome)
        evidence.pop("ok", None)
        return {"ok": True, "evidence": evidence}

    return {
        RagDagNodeType.INDEX: _index if index_fn is not None else _missing("index"),
        RagDagNodeType.VERIFICATION: (
            _verification if verify_fn is not None else _missing("verification")
        ),
        RagDagNodeType.PUBLISH_BARRIER: (
            _publish_barrier if publish_fn is not None else _missing("publish-barrier")
        ),
        RagDagNodeType.REPAIR: _repair if repair_fn is not None else _missing("repair"),
    }


__all__ = [
    "query_chain_handlers",
    "retrieval_chain_handlers",
    "write_path_handlers",
]
