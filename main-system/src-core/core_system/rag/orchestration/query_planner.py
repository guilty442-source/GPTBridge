"""RAG Query Planner — chooses architectures; multi-arch allowed.

Two plan modes:

- STATIC: simple questions route directly to one or more retrievers
  (no router-model call, lowest latency).  Default is ``hybrid-rag`` —
  cheapest, most stable, most common path.
- ADAPTIVE: complex questions get an ``agentic-rag`` orchestration
  plan that iterates through the sufficiency gate.

``rag_architectures`` is a tuple — a query like "continue yesterday's
RAG takeover and find the code" needs memory + code + hybrid at once.

Architecture routing is fully separated from generation routing:
``generation_mode`` is passed through opaquely (see
``generation_router.py``); picking ``code-rag`` retrieval never
implies the qwen3-coder model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .evidence import DEFAULT_ARCHITECTURE, RagArchitecture


class PlanMode(str, Enum):
    STATIC = "STATIC"        # direct retriever dispatch
    ADAPTIVE = "ADAPTIVE"    # agentic orchestration with sufficiency gate


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """The planner's executable decision — fully auditable."""

    plan_id: str
    query: str
    mode: PlanMode
    rag_architectures: tuple[RagArchitecture, ...]
    generation_mode: str = "general"
    module_ids: tuple[str, ...] = ()
    session_id: str = ""
    max_rounds: int = 3
    top_k: int = 6
    reasons: tuple[str, ...] = ()


# Note: CJK terms can't use \b (adjacent CJK chars are all word
# characters); they match as a plain alternation instead.
_CODE_HINT = re.compile(
    r"\b(?:class|def|function|import|method|module|file|symbol|"
    r"reference|dependency|calls|inherits|implements)\b"
    r"|影響|呼叫|引用|依賴|函式|類別|檔案|程式碼"
    r"|[A-Z][a-zA-Z0-9]+(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+",
)
_MEMORY_HINT = re.compile(
    r"\b(?:yesterday|last time|earlier|before|previously|remember)\b"
    r"|昨天|上次|之前|剛才|繼續",
    re.IGNORECASE,
)
_COMPLEX_HINT = re.compile(
    r"\b(?:why|analyze|analyse|diagnose|debug|root cause|compare)\b"
    r"|為什麼|分析|診斷|比較|排查|找出",
    re.IGNORECASE,
)


def plan_query(
    query: str,
    *,
    plan_id: str = "",
    generation_mode: str = "general",
    module_ids: tuple[str, ...] = (),
    session_id: str = "",
    explicit_architectures: tuple[RagArchitecture, ...] | None = None,
    max_rounds: int = 3,
) -> QueryPlan:
    """Produce a static or adaptive plan.

    ``explicit_architectures`` overrides detection entirely (caller
    governance decision).  Otherwise hints select architectures;
    nothing detected -> ``hybrid-rag`` (the default retriever).
    """
    reasons: list[str] = []

    if explicit_architectures:
        archs = tuple(dict.fromkeys(explicit_architectures))
        reasons.append("explicit-architectures")
    else:
        arch_list: list[RagArchitecture] = []
        if _CODE_HINT.search(query):
            arch_list.append(RagArchitecture.CODE)
            reasons.append("code-hint")
        if _MEMORY_HINT.search(query):
            arch_list.append(RagArchitecture.MEMORY)
            reasons.append("memory-hint")
        if not arch_list:
            arch_list.append(RagArchitecture.HYBRID)
            reasons.append("default-hybrid")
        elif RagArchitecture.HYBRID not in arch_list:
            # multi-arch queries still get hybrid as evidence baseline
            arch_list.append(RagArchitecture.HYBRID)
            reasons.append("hybrid-baseline")
        archs = tuple(arch_list)

    adaptive = bool(_COMPLEX_HINT.search(query)) or (
        RagArchitecture.AGENTIC in archs
    )
    if adaptive:
        archs = tuple(a for a in archs if a is not RagArchitecture.AGENTIC)
        if not archs:
            archs = (DEFAULT_ARCHITECTURE,)
        reasons.append("adaptive-mode")
        mode = PlanMode.ADAPTIVE
    else:
        mode = PlanMode.STATIC

    return QueryPlan(
        plan_id=plan_id,
        query=query,
        mode=mode,
        rag_architectures=archs,
        generation_mode=generation_mode,
        module_ids=module_ids,
        session_id=session_id,
        max_rounds=max_rounds,
        reasons=tuple(reasons),
    )


__all__ = ["PlanMode", "QueryPlan", "plan_query"]
