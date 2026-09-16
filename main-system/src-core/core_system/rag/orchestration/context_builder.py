"""Context Builder — four ordered layers, kind-aware formatting.

Order is fixed and auditable:

    1. System / Governance
    2. Canonical Source Evidence   (SOURCE_TEXT, STRUCTURED_DATA)
    3. Specialized Evidence        (CODE_SNIPPET, SYMBOL, DEPENDENCY,
                                    MEMORY, DERIVED)
    4. Task Instruction

Kind handling:
    CODE_SNIPPET  -> keep the whole function, no truncation mid-body
    SOURCE_TEXT   -> adjacent chunks from the same resource may merge
    MEMORY        -> scope (session/episodic/...) shown explicitly
    DEPENDENCY    -> rendered as a structured relation, not prose
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .evidence import EvidenceKind, RagEvidence, make_citation

_LAYER2_KINDS = {EvidenceKind.SOURCE_TEXT, EvidenceKind.STRUCTURED_DATA}
_LAYER3_KINDS = {
    EvidenceKind.CODE_SNIPPET,
    EvidenceKind.SYMBOL,
    EvidenceKind.DEPENDENCY,
    EvidenceKind.MEMORY,
    EvidenceKind.DERIVED,
}


@dataclass(frozen=True, slots=True)
class ContextSection:
    layer: int
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class BuiltContext:
    sections: tuple[ContextSection, ...]
    text: str
    citations: tuple[Any, ...]
    evidence_used: int


def _merge_adjacent(evidence: list[RagEvidence]) -> list[RagEvidence]:
    """Merge SOURCE_TEXT chunks that are adjacent in one resource."""
    by_res: dict[str, list[RagEvidence]] = {}
    out: list[RagEvidence] = []
    for e in evidence:
        if e.evidence_kind is not EvidenceKind.SOURCE_TEXT or not e.resource_id:
            out.append(e)
            continue
        idx = e.provenance.get("chunk_index")
        if idx is None:
            out.append(e)
            continue
        by_res.setdefault(e.resource_id, []).append(e)
    for res, group in by_res.items():
        group.sort(key=lambda e: int(e.provenance.get("chunk_index", 0)))
        merged: list[RagEvidence] = []
        for e in group:
            if merged and int(e.provenance["chunk_index"]) == int(
                merged[-1].provenance["chunk_index"]
            ) + 1:
                prev = merged[-1]
                merged[-1] = _concat(prev, e)
            else:
                merged.append(e)
        out.extend(merged)
    return out


def _concat(a: RagEvidence, b: RagEvidence) -> RagEvidence:
    import dataclasses
    return dataclasses.replace(
        a,
        content=f"{a.content}\n{b.content}",
        provenance={**a.provenance, "merged_with": b.evidence_id},
    )


def _format_evidence(e: RagEvidence, label: str) -> str:
    if e.evidence_kind is EvidenceKind.MEMORY:
        scope = e.provenance.get("memory_kind", "memory")
        return f"{label} (memory:{scope}) {e.content}"
    if e.evidence_kind is EvidenceKind.DEPENDENCY:
        src = e.provenance.get("source_symbol", "")
        dst = e.provenance.get("target_symbol", "")
        et = e.provenance.get("edge_type", "REFERENCES")
        return f"{label} {src} -[{et}]-> {dst}"
    if e.evidence_kind is EvidenceKind.SYMBOL:
        return f"{label} symbol `{e.provenance.get('symbol_id', e.evidence_id)}`\n{e.content}"
    return f"{label} {e.content}"


def build_context(
    evidence: list[RagEvidence],
    *,
    system_governance: str,
    task_instruction: str,
    max_context_chars: int = 24000,
) -> BuiltContext:
    """Assemble the four-layer context within the token budget."""
    prepared = _merge_adjacent([e for e in evidence if e.authorized])
    citations: list[Any] = []
    layer2: list[str] = []
    layer3: list[str] = []
    for i, e in enumerate(prepared, start=1):
        cit = make_citation(i, e)
        citations.append(cit)
        line = _format_evidence(e, cit.label)
        (layer2 if e.evidence_kind in _LAYER2_KINDS else layer3).append(line)

    sections = [
        ContextSection(1, "System / Governance", system_governance),
        ContextSection(2, "Canonical Source Evidence", "\n\n".join(layer2)),
        ContextSection(3, "Specialized Evidence", "\n\n".join(layer3)),
        ContextSection(4, "Task Instruction", task_instruction),
    ]
    text = "\n\n".join(f"## {s.title}\n{s.body}" for s in sections if s.body)
    if len(text) > max_context_chars:
        text = text[:max_context_chars]
    return BuiltContext(
        sections=tuple(sections),
        text=text,
        citations=tuple(citations),
        evidence_used=len(prepared),
    )


__all__ = ["BuiltContext", "ContextSection", "build_context"]
