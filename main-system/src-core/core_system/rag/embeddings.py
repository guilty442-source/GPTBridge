"""Embedding Service — Generate embeddings for RAG pipeline.

Supports OpenAI-compatible API and local embedding models.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI


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
    """OpenAI-compatible embedding provider."""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
    ) -> None:
        self._model = model
        self._dimension = dimension
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
    """Ollama local embedding provider."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        dimension: int = 768,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimension = dimension
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
        embeddings = []
        for text in texts:
            response = await self._client.post(
                f"{self._base_url}/api/embeddings",
                json={"model": self._model, "prompt": text},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            embeddings.append(data["embedding"])
        return embeddings

    async def health_check(self) -> bool:
        try:
            await self.embed(["health check"])
            return True
        except Exception:
            return False


class LocalEmbeddingProvider(EmbeddingProvider):
    """Local sentence-transformers embedding provider (fallback)."""

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

    def _load_model(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            self._dimension = self._model.get_sentence_embedding_dimension()

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
    """Create embedding provider from environment variables."""
    provider = os.environ.get("EMBEDDING_PROVIDER", "openai").lower()

    if provider == "ollama":
        return OllamaEmbeddingProvider(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.environ.get("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"),
            dimension=int(os.environ.get("EMBEDDING_DIMENSION", "768")),
        )
    elif provider == "local":
        return LocalEmbeddingProvider(
            model=os.environ.get("LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        )
    else:
        # Default to OpenAI-compatible
        return OpenAIEmbeddingProvider(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL"),
            model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
            dimension=int(os.environ.get("EMBEDDING_DIMENSION", "1536")),
        )


__all__ = [
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "LocalEmbeddingProvider",
    "create_embedding_provider_from_env",
]