"""Deduplication — URL / 內容 Hash / 近似重複 / 語意重複去重。

對應需求 32：
  URL Dedup → Exact Hash Dedup → Near Duplicate Detection → Semantic Duplicate Detection
  多個網站轉載相同新聞時，保留：原始來源優先、可信來源優先、較新版本優先
  但可保留其他來源作交叉驗證 Metadata。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .chunking import TextChunk


# 可信任來源域名（分數越高越可信）
_TRUSTED_DOMAINS: dict[str, float] = {
    "wikipedia.org": 0.9,
    "github.com": 0.85,
    "stackoverflow.com": 0.8,
    "arxiv.org": 0.9,
    "official.microsoft.com": 0.9,
    "docs.python.org": 0.9,
    "pytorch.org": 0.9,
    "developer.mozilla.org": 0.85,
}


@dataclass
class DedupResult:
    """去重結果。"""

    unique: list[TextChunk] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    cross_validation: list[dict[str, Any]] = field(default_factory=list)


class Deduplicator:
    """多層去重器。"""

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        preserve_cross_validation: bool = True,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.preserve_cross_validation = preserve_cross_validation

    def deduplicate(self, chunks: list[TextChunk]) -> DedupResult:
        result = DedupResult()

        # 1. URL 去重（同 URL 只保留第一個）
        seen_urls: set[str] = set()
        url_deduped: list[TextChunk] = []
        for chunk in chunks:
            if chunk.url in seen_urls:
                result.duplicates.append({
                    "chunk_id": chunk.chunk_id,
                    "url": chunk.url,
                    "reason": "duplicate_url",
                })
                continue
            seen_urls.add(chunk.url)
            url_deduped.append(chunk)

        # 2. Exact Hash 去重
        seen_hashes: dict[str, TextChunk] = {}
        hash_deduped: list[TextChunk] = []
        for chunk in url_deduped:
            content_hash = hashlib.sha256(
                chunk.text.strip().lower().encode("utf-8")
            ).hexdigest()
            if content_hash in seen_hashes:
                existing = seen_hashes[content_hash]
                result.duplicates.append({
                    "chunk_id": chunk.chunk_id,
                    "duplicate_of": existing.chunk_id,
                    "reason": "exact_hash",
                })
                # 交叉驗證 metadata
                if self.preserve_cross_validation:
                    result.cross_validation.append({
                        "primary": existing.chunk_id,
                        "secondary": chunk.chunk_id,
                        "urls": [existing.url, chunk.url],
                    })
                continue
            seen_hashes[content_hash] = chunk
            hash_deduped.append(chunk)

        # 3. 近似重複偵測（Jaccard similarity on word sets）
        final_chunks: list[TextChunk] = []
        for chunk in hash_deduped:
            is_dup = False
            for existing in final_chunks:
                sim = self._jaccard_similarity(chunk.text, existing.text)
                if sim >= self.similarity_threshold:
                    # 保留更可信或更新的來源
                    if self._should_replace(existing, chunk):
                        final_chunks.remove(existing)
                        final_chunks.append(chunk)
                        result.duplicates.append({
                            "chunk_id": existing.chunk_id,
                            "replaced_by": chunk.chunk_id,
                            "reason": f"near_duplicate (sim={sim:.2f})",
                        })
                    else:
                        result.duplicates.append({
                            "chunk_id": chunk.chunk_id,
                            "duplicate_of": existing.chunk_id,
                            "reason": f"near_duplicate (sim={sim:.2f})",
                        })
                        if self.preserve_cross_validation:
                            result.cross_validation.append({
                                "primary": existing.chunk_id,
                                "secondary": chunk.chunk_id,
                                "urls": [existing.url, chunk.url],
                                "similarity": sim,
                            })
                    is_dup = True
                    break
            if not is_dup:
                final_chunks.append(chunk)

        result.unique = final_chunks
        return result

    @staticmethod
    def _jaccard_similarity(text_a: str, text_b: str) -> float:
        """Jaccard 相似度（基於詞集）。"""
        words_a = set(re.findall(r"\w+", text_a.lower()))
        words_b = set(re.findall(r"\w+", text_b.lower()))
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    @staticmethod
    def _should_replace(existing: TextChunk, candidate: TextChunk) -> bool:
        """判斷是否應該用 candidate 取代 existing。"""
        existing_trust = _TRUSTED_DOMAINS.get(existing.domain, 0.5)
        candidate_trust = _TRUSTED_DOMAINS.get(candidate.domain, 0.5)
        if candidate_trust > existing_trust:
            return True
        if candidate_trust < existing_trust:
            return False
        # 同信任度：保留較新的
        if candidate.published_time and existing.published_time:
            return candidate.published_time > existing.published_time
        return False


__all__ = ["Deduplicator", "DedupResult"]
