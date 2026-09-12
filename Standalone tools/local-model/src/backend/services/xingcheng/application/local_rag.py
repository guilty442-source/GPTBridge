from __future__ import annotations

from .local_rag_constants import LocalRagConstants
from .local_rag_documents import LocalRagDocumentsMixin
from .local_rag_index import LocalRagIndexMixin
from .local_rag_retrieval import LocalRagRetrievalMixin


class LocalRagService(
    LocalRagConstants,
    LocalRagDocumentsMixin,
    LocalRagIndexMixin,
    LocalRagRetrievalMixin,
):
    """Governed hybrid retrieval over centrally labelled, module-owned data.

    Codex basis:
      A52/E38 — RAG architecture: hybrid-rag + code-rag + agentic-rag + memory-rag.
                 This implementation provides the *execution* surface for all four
                 sub-architectures.  The *declaration* lives in ``src/rag/`` (A2:
                 pure-declaration; A5: execution delegated to governed-executor).
      A8/E21  — Qdrant is the canonical semantic index; LocalVectorStore is a
                 bounded degraded cache only (FORBID: local-vector-as-canonical).
      A44/E30 — Fallback: sqlite-private + local-vector-cache + bounded +
                 observable + reconciled + non-canonical.  This service operates
                 in degraded mode when Qdrant is unavailable; all results are
                 non-canonical and reconciliation_required=True.
      A49/E35 — Formal-tools: Qdrant is a formal tool; implementation dependencies
                 (LocalVectorStore, SQLite FTS) are approved-inventory, NOT role
                 authority — they do not replace Qdrant's canonical role.
    """


__all__ = ["LocalRagService"]
