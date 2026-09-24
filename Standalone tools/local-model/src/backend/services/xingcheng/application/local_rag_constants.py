from __future__ import annotations

from pathlib import Path
from typing import Any

from ..infrastructure.local_rag_canonical import CanonicalRagAdapter
from shared_layer.local.local_sqlite_rag_repository import LocalSqliteRagRepository
from shared_layer.local.vector_store import LocalVectorStore
from ..infrastructure.qwen_reranker import QwenReranker


class LocalRagConstants:
    """Class-level constants and constructor for LocalRagService."""

    MAX_FILES = 256
    MAX_FILE_BYTES = 4_000_000
    MAX_DOCUMENT_CHARACTERS = 2_000_000
    CHUNK_CHARACTERS = 1_200
    CHUNK_OVERLAP = 200
    EMBEDDING_BATCH_SIZE = 32
    ROUTER_MODEL = "xingcheng-native-transformer"
    RAG_MODELS = {
        "general": ("xingcheng-native-transformer",),
        "fast": ("xingcheng-native-transformer",),
        "code": ("xingcheng-native-transformer",),
        "deep": ("xingcheng-native-transformer",),
        "visual": ("xingcheng-native-transformer",),
    }
    FALLBACK_MODEL = "xingcheng-native-transformer"
    SUPPORTED_SUFFIXES = frozenset(
        {
            ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json",
            ".jsonl", ".html", ".htm", ".xml", ".yaml", ".yml", ".toml",
            ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".sql", ".docx",
        }
    )

    def __init__(
        self,
        tool_root: Path,
        native_runtime: Any,
        *,
        vector_store: LocalVectorStore | None = None,
        reranker: QwenReranker | None = None,
        repository: LocalSqliteRagRepository | None = None,
        canonical: Any = None,
    ) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.project_root = self.tool_root.parent.resolve()
        self.native_runtime = native_runtime
        self.repository = repository or LocalSqliteRagRepository(self.tool_root)
        self.vector_store = vector_store or LocalVectorStore(
            self.tool_root / "runtime" / "state" / "local-rag-vectors.sqlite3"
        )
        self.reranker = reranker or QwenReranker()
        # A371-A374: canonical path adapter.  Constructed lazily — it only
        # opens its background loop when first probed, so environments without
        # Qdrant/PostgreSQL never pay for it and stay on the degraded path.
        self.canonical = (
            canonical
            if canonical is not None
            else CanonicalRagAdapter(
                self.tool_root,
                native_runtime,
                document_fetcher=self._reconcile_source_document,
            )
        )
