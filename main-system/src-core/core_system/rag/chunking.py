"""Chunking Service — Split documents into chunks for RAG indexing.

Implements configurable chunking with overlap and metadata preservation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import tiktoken


@dataclass(frozen=True)
class Chunk:
    """A document chunk with metadata."""
    content: str
    index: int
    start_char: int
    end_char: int
    token_count: int
    metadata: dict[str, Any]


class ChunkingStrategy:
    """Base chunking strategy."""

    def chunk(self, text: str, metadata: dict[str, Any]) -> list[Chunk]:
        raise NotImplementedError


class FixedSizeChunking(ChunkingStrategy):
    """Fixed-size token-based chunking with overlap."""

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        encoding_name: str = "cl100k_base",
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.encoding = tiktoken.get_encoding(encoding_name)

    def count_tokens(self, text: str) -> int:
        return len(self.encoding.encode(text))

    def chunk(self, text: str, metadata: dict[str, Any]) -> list[Chunk]:
        if not text.strip():
            return []

        tokens = self.encoding.encode(text)
        if len(tokens) <= self.chunk_size:
            return [Chunk(
                content=text,
                index=0,
                start_char=0,
                end_char=len(text),
                token_count=len(tokens),
                metadata=metadata,
            )]

        chunks = []
        start_token = 0
        chunk_index = 0

        while start_token < len(tokens):
            end_token = min(start_token + self.chunk_size, len(tokens))
            chunk_tokens = tokens[start_token:end_token]
            chunk_text = self.encoding.decode(chunk_tokens)

            # Calculate character positions (approximate)
            char_start = len(self.encoding.decode(tokens[:start_token]))
            char_end = char_start + len(chunk_text)

            chunks.append(Chunk(
                content=chunk_text,
                index=chunk_index,
                start_char=char_start,
                end_char=char_end,
                token_count=len(chunk_tokens),
                metadata={**metadata, "chunk_index": chunk_index},
            ))

            chunk_index += 1
            if end_token >= len(tokens):
                break
            start_token = end_token - self.chunk_overlap

        return chunks


class SemanticChunking(ChunkingStrategy):
    """Semantic chunking based on paragraph/sentence boundaries."""

    def __init__(
        self,
        max_chunk_size: int = 512,
        min_chunk_size: int = 100,
        encoding_name: str = "cl100k_base",
    ) -> None:
        self.max_chunk_size = max_chunk_size
        self.min_chunk_size = min_chunk_size
        self.encoding = tiktoken.get_encoding(encoding_name)

    def count_tokens(self, text: str) -> int:
        return len(self.encoding.encode(text))

    def _split_into_sentences(self, text: str) -> list[str]:
        """Split text into sentences."""
        # Simple sentence splitting - can be improved with spaCy/NLTK
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if s.strip()]

    def chunk(self, text: str, metadata: dict[str, Any]) -> list[Chunk]:
        if not text.strip():
            return []

        sentences = self._split_into_sentences(text)
        chunks = []
        current_chunk = ""
        current_tokens = 0
        chunk_index = 0
        char_pos = 0

        for sentence in sentences:
            sentence_tokens = self.count_tokens(sentence)

            if current_tokens + sentence_tokens > self.max_chunk_size and current_tokens >= self.min_chunk_size:
                # Finalize current chunk
                chunk = Chunk(
                    content=current_chunk.strip(),
                    index=chunk_index,
                    start_char=char_pos - len(current_chunk),
                    end_char=char_pos,
                    token_count=current_tokens,
                    metadata={**metadata, "chunk_index": chunk_index},
                )
                chunks.append(chunk)

                chunk_index += 1
                current_chunk = sentence
                current_tokens = sentence_tokens
                char_pos += len(current_chunk) + 1
            else:
                if current_chunk:
                    current_chunk += " "
                current_chunk += sentence
                current_tokens += sentence_tokens
                char_pos += len(sentence) + 1

        # Add final chunk
        if current_chunk.strip() and current_tokens > 0:
            chunks.append(Chunk(
                content=current_chunk.strip(),
                index=chunk_index,
                start_char=char_pos - len(current_chunk),
                end_char=char_pos,
                token_count=current_tokens,
                metadata={**metadata, "chunk_index": chunk_index},
            ))

        return chunks


def create_chunker(
    strategy: str = "fixed",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    **kwargs,
) -> ChunkingStrategy:
    """Factory for chunking strategies."""
    if strategy == "fixed":
        return FixedSizeChunking(chunk_size=chunk_size, chunk_overlap=chunk_overlap, **kwargs)
    elif strategy == "semantic":
        return SemanticChunking(max_chunk_size=chunk_size, **kwargs)
    else:
        raise ValueError(f"Unknown chunking strategy: {strategy}")


@dataclass(frozen=True)
class Document:
    """Source document for chunking."""
    resource_id: str
    module_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ChunkingService:
    """Service for chunking documents for RAG indexing."""

    def __init__(self, strategy: ChunkingStrategy) -> None:
        self.strategy = strategy

    def chunk_document(self, doc: Document) -> list[Chunk]:
        """Chunk a single document."""
        base_metadata = {
            "resource_id": doc.resource_id,
            "module_id": doc.module_id,
            **doc.metadata,
        }
        return self.strategy.chunk(doc.content, base_metadata)

    def chunk_documents(self, docs: list[Document]) -> list[Chunk]:
        """Chunk multiple documents."""
        all_chunks = []
        for doc in docs:
            all_chunks.extend(self.chunk_document(doc))
        return all_chunks


def create_chunking_service_from_env() -> ChunkingService:
    """Create chunking service from environment variables."""
    strategy = os.environ.get("CHUNKING_STRATEGY", "fixed").lower()
    chunk_size = int(os.environ.get("CHUNK_SIZE", "512"))
    chunk_overlap = int(os.environ.get("CHUNK_OVERLAP", "64"))

    if strategy == "semantic":
        chunker = SemanticChunking(max_chunk_size=chunk_size)
    else:
        chunker = FixedSizeChunking(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    return ChunkingService(chunker)


__all__ = [
    "Chunk",
    "Document",
    "ChunkingStrategy",
    "FixedSizeChunking",
    "SemanticChunking",
    "ChunkingService",
    "create_chunker",
    "create_chunking_service_from_env",
]