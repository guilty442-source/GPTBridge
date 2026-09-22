"""Embedding Service — Generate embeddings for RAG pipeline.

Supports OpenAI-compatible API and local embedding models.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any, Optional


class EmbeddingProvider(ABC):
    """Abstract embedding provider."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        pass

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider (explicit opt-in only).

    Canonical contract is qwen3-embedding:4b / 2560-dim via the local Ollama
    runtime; this provider is never a default path and its defaults match
    the canonical contract so an uninstantiated override can never produce
    an incompatible-dimension vector for the canonical collection.
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "qwen3-embedding:4b",
        dimension: int = 2560,
    ) -> None:
        self._model = model
        self._dimension = dimension
        # Lazy import: openai pulls in httpx, pydantic, and other heavy
        # dependencies.  Deferring it to first instantiation keeps cold
        # startup fast for modules that only need the type definitions.
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
        )
        return [d.embedding for d in response.data]

    async def health_check(self) -> bool:
        try:
            await self.embed(["health check"])
            return True
        except Exception:
            return False


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Ollama local embedding provider (governed canonical default)."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3-embedding:4b",
        dimension: int = 2560,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimension = dimension
        # Lazy import: httpx is only needed when actually making requests.
        import httpx
        self._client = httpx.AsyncClient(timeout=60.0)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": texts},
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        return data["embeddings"]

    async def health_check(self) -> bool:
        try:
            await self.embed(["health check"])
            return True
        except Exception:
            return False


class LocalEmbeddingProvider(EmbeddingProvider):
    """Local sentence-transformers embedding provider (non-canonical fallback).

    Output dimension follows the loaded model and is NOT the canonical
    2560-dim contract — this provider may only serve degraded/test paths and
    must never feed the canonical Qdrant collection.
    """

    def __init__(self, model: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model
        self._model = None
        self._dimension = 384  # Default for MiniLM

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    # §10.7：embedding 屬 on_demand 角色，載入前經受管資源閘門。
    # all-MiniLM-L6-v2 權重 ~90MB；512MB 為含執行期 overhead 之保守估測。
    _EMBEDDING_RAM_REQUIRED_MB = 512

    def _load_model(self) -> None:
        if self._model is None:
            from core_system.model_resource_manager import (
                ModelRole,
                get_model_resource_manager,
            )

            mgr = get_model_resource_manager()
            model_id = f"sentence-transformers/{self._model_name}"
            decision = mgr.request_load(
                ModelRole.EMBEDDING,
                model_id,
                ram_mb=self._EMBEDDING_RAM_REQUIRED_MB,
            )
            if not decision.admitted:
                raise RuntimeError(
                    f"embedding load denied by resource gate: {decision.reason}"
                )
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self._model_name)
                self._dimension = self._model.get_sentence_embedding_dimension()
            except Exception:
                mgr.release(model_id)
                raise

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load_model()
        # Run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, self._model.encode, texts)
        return [e.tolist() for e in embeddings]

    async def health_check(self) -> bool:
        try:
            await self.embed(["health check"])
            return True
        except Exception:
            return False


def create_embedding_provider_from_env() -> EmbeddingProvider:
    """Create embedding provider from environment variables.

    The governed canonical default is the local Ollama runtime serving
    qwen3-embedding:4b (2560-dim).  Remote providers are only reachable by
    explicit EMBEDDING_PROVIDER override and are never the default path.
    """
    provider = os.environ.get("EMBEDDING_PROVIDER", "ollama").lower()

    if provider == "ollama":
        return OllamaEmbeddingProvider(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:4b"),
            dimension=int(os.environ.get("EMBEDDING_DIMENSION", "2560")),
        )
    elif provider == "local":
        return LocalEmbeddingProvider(
            model=os.environ.get("LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        )
    else:
        # Explicit opt-in only: OpenAI-compatible endpoint.
        return OpenAIEmbeddingProvider(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("EMBEDDING_MODEL", "qwen3-embedding:4b"),
            dimension=int(os.environ.get("EMBEDDING_DIMENSION", "2560")),
        )


__all__ = [
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "LocalEmbeddingProvider",
    "create_embedding_provider_from_env",
]