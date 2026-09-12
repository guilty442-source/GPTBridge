from __future__ import annotations

from pathlib import Path
from typing import Any

from ..infrastructure.local_sqlite_rag_repository import LocalSqliteRagRepository
from ..infrastructure.vector_store import LocalVectorStore
from ..infrastructure.qwen_reranker import QwenReranker


class LocalRagConstants:
    """Class-level constants and constructor for LocalRagService."""

    MAX_FILES = 256
    MAX_FILE_BYTES = 4_000_000
    MAX_DOCUMENT_CHARACTERS = 2_000_000
    CHUNK_CHARACTERS = 1_200
    CHUNK_OVERLAP = 200
    EMBEDDING_BATCH_SIZE = 32
    ROUTER_MODEL = "nemotron-3-nano:4b-q8_0"
    RAG_MODELS = {
        "general": ("qwen3.8:27b-q4_K_M",),
        "fast": ("gemma4:12b-it-qat",),
        "code": ("qwen3-coder:30b-a3b-q4_K_M",),
        "deep": ("ornith-1.5:35b", "deepseek-r1:14b"),
        "visual": ("qwen3-vl:8b-thinking",),
    }
    FALLBACK_MODEL = "mistral-small:24b"
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
        transformer_runtime: Any,
        *,
        vector_store: LocalVectorStore | None = None,
        reranker: QwenReranker | None = None,
        repository: LocalSqliteRagRepository | None = None,
    ) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.project_root = self.tool_root.parent.resolve()
        self.transformer_runtime = transformer_runtime
        self.repository = repository or LocalSqliteRagRepository(self.tool_root)
        self.vector_store = vector_store or LocalVectorStore(
            self.tool_root / "runtime" / "state" / "local-rag-vectors.sqlite3"
        )
        self.reranker = reranker or QwenReranker()
