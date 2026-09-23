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


__all__ = ["retrieval_chain_handlers"]
