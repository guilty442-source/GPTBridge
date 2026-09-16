"""RAG orchestration layer — the formal relationship between the four
sub-architectures (hybrid-rag / code-rag / memory-rag / agentic-rag).

    Query -> Scope Resolver -> Query Planner
        -> Hybrid / Code / Memory retrievers (capabilities)
        -> Evidence Pool -> Evidence Fusion -> Reranker
        -> Evidence Sufficiency
            YES -> Context Builder -> Generation Router -> LLM
            NO  -> Agentic Controller (reformulate / retry)

Hybrid, Code and Memory are retrieval capabilities; Agentic is the
adaptive orchestration capability — it may only call the formal
retrievers, never raw stores.
"""
from .agentic_controller import ALLOWED_TOOLS, AgenticController, AgenticResult
from .context_builder import BuiltContext, build_context
from .evidence import (
    AUTHORITY_RANK,
    DEFAULT_ARCHITECTURE,
    Citation,
    EvidenceKind,
    MemoryKind,
    RagArchitecture,
    RagEvidence,
    SourceAuthority,
    authority_of,
    make_citation,
)
from .fusion import (
    architecture_fusion,
    channel_fusion_code,
    channel_fusion_hybrid,
    channel_fusion_memory,
    mark_conflicts,
    memory_score,
)
from .generation_router import GenerationMode, route_generation
from .orchestrator import OrchestratorResult, RagOrchestrator
from .query_planner import PlanMode, QueryPlan, plan_query
from .sufficiency import (
    SufficiencyPolicy,
    SufficiencyReport,
    SufficiencyVerdict,
    evaluate_sufficiency,
)
from .symbols import (
    CodeEdge,
    CodeSymbol,
    EdgeType,
    SymbolIdentity,
    SymbolKind,
    make_symbol_id,
    valid_symbol_id,
)

__all__ = [
    "ALLOWED_TOOLS",
    "AUTHORITY_RANK",
    "AgenticController",
    "AgenticResult",
    "BuiltContext",
    "Citation",
    "CodeEdge",
    "CodeSymbol",
    "DEFAULT_ARCHITECTURE",
    "EdgeType",
    "EvidenceKind",
    "GenerationMode",
    "MemoryKind",
    "OrchestratorResult",
    "PlanMode",
    "QueryPlan",
    "RagArchitecture",
    "RagEvidence",
    "RagOrchestrator",
    "SourceAuthority",
    "SufficiencyPolicy",
    "SufficiencyReport",
    "SufficiencyVerdict",
    "SymbolIdentity",
    "SymbolKind",
    "architecture_fusion",
    "authority_of",
    "build_context",
    "channel_fusion_code",
    "channel_fusion_hybrid",
    "channel_fusion_memory",
    "evaluate_sufficiency",
    "make_citation",
    "make_symbol_id",
    "mark_conflicts",
    "memory_score",
    "plan_query",
    "route_generation",
    "valid_symbol_id",
]
