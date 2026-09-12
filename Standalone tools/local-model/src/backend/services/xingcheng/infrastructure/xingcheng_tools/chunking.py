"""Content Chunking — 將大型網頁內容切分為適合模型處理的 Chunk。

對應需求 31：
  每個 Chunk 保留：Document ID、Chunk ID、URL、Title、Domain、
  Published Time、Section、Position、Text、Token Estimate
  Chunk 大小需依星澄 Context Window 動態控制。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from .parser.cleaner import CleanDocument


@dataclass
class TextChunk:
    """單一文字片段。"""

    chunk_id: str
    document_id: str
    url: str
    title: str
    domain: str
    published_time: str | None = None
    section: str = ""
    position: int = 0
    text: str = ""
    token_estimate: int = 0
    fetched_time: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "url": self.url,
            "title": self.title,
            "domain": self.domain,
            "published_time": self.published_time,
            "section": self.section,
            "position": self.position,
            "token_estimate": self.token_estimate,
            "fetched_time": self.fetched_time,
        }


class Chunker:
    """內容切分器。"""

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        max_chunks_per_doc: int = 10,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_chunks_per_doc = max_chunks_per_doc

    def chunk(self, doc: CleanDocument, *, context_window: int | None = None) -> list[TextChunk]:
        """將 CleanDocument 切分為 TextChunk 列表。"""
        # 依 context window 動態調整 chunk size
        effective_size = self.chunk_size
        if context_window and context_window < 2048:
            effective_size = min(self.chunk_size, context_window // 4)

        document_id = hashlib.sha256(doc.url.encode("utf-8")).hexdigest()[:16]
        domain = self._extract_domain(doc.url)

        # 以段落為基礎切分
        paragraphs = doc.text.split("\n\n")
        chunks: list[TextChunk] = []
        current_text = ""
        current_section = ""
        position = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # 段落本身可能就是一個 chunk
            if len(para) > effective_size:
                # 先儲存累積的內容
                if current_text:
                    chunks.append(self._make_chunk(
                        document_id, doc, domain, current_section, position, current_text
                    ))
                    position += 1
                    current_text = ""
                # 長段落用滑動窗口切分
                for sub in self._split_long_text(para, effective_size, self.chunk_overlap):
                    chunks.append(self._make_chunk(
                        document_id, doc, domain, current_section, position, sub
                    ))
                    position += 1
            else:
                if len(current_text) + len(para) > effective_size:
                    chunks.append(self._make_chunk(
                        document_id, doc, domain, current_section, position, current_text
                    ))
                    position += 1
                    current_text = para
                else:
                    current_text = f"{current_text}\n\n{para}" if current_text else para

        if current_text:
            chunks.append(self._make_chunk(
                document_id, doc, domain, current_section, position, current_text
            ))
            position += 1

        return chunks[: self.max_chunks_per_doc]

    def _make_chunk(
        self,
        document_id: str,
        doc: CleanDocument,
        domain: str,
        section: str,
        position: int,
        text: str,
    ) -> TextChunk:
        chunk_id = hashlib.sha256(
            f"{document_id}|{position}|{text[:50]}".encode("utf-8")
        ).hexdigest()[:16]
        return TextChunk(
            chunk_id=chunk_id,
            document_id=document_id,
            url=doc.url,
            title=doc.title,
            domain=domain,
            published_time=doc.published_time,
            section=section,
            position=position,
            text=text.strip(),
            token_estimate=self._estimate_tokens(text),
        )

    @staticmethod
    def _split_long_text(text: str, size: int, overlap: int) -> list[str]:
        """用滑動窗口切分長文字。"""
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + size
            chunks.append(text[start:end])
            start = end - overlap
        return chunks

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 數（中文 ~1 字/token，英文 ~4 字元/token）。"""
        chinese_chars = sum(1 for c in text if "\u3400" <= c <= "\u9fff")
        other_chars = len(text) - chinese_chars
        return chinese_chars + other_chars // 4

    @staticmethod
    def _extract_domain(url: str) -> str:
        from urllib.parse import urlparse
        try:
            return urlparse(url).netloc or ""
        except Exception:
            return ""


__all__ = ["Chunker", "TextChunk"]
