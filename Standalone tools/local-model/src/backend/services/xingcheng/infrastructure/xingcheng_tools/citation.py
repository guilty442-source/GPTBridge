"""Citation Resolver + Source Formatter — 引用映射與來源格式化。

對應需求 37, 40-41：
  Answer Segment → Source ID → URL / Title / Domain
  Source Formatter 統一產生引用
  模型本身不需要直接拼接 URL 字串
  回傳使用者流程：星澄模型答案 → Citation Resolver → Source Formatter
    → Response Validator → 使用者
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .source import SourceMetadata


@dataclass
class Citation:
    """單一引用。"""

    citation_id: int
    source_id: str
    url: str | None = None
    title: str | None = None
    domain: str | None = None
    published_time: str | None = None

    def format(self) -> str:
        parts = [f"[{self.citation_id}]"]
        if self.title:
            parts.append(self.title)
        if self.url:
            parts.append(self.url)
        elif self.domain:
            parts.append(self.domain)
        if self.published_time:
            parts.append(f"({self.published_time})")
        return " ".join(parts)


@dataclass
class CitationResult:
    """引用解析結果。"""

    answer_with_citations: str = ""
    citations: list[Citation] = field(default_factory=list)
    unresolved_references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_with_citations": self.answer_with_citations,
            "citation_count": len(self.citations),
            "citations": [
                {"id": c.citation_id, "source_id": c.source_id, "url": c.url, "title": c.title}
                for c in self.citations
            ],
            "unresolved": self.unresolved_references,
        }


class CitationResolver:
    """引用解析器 — 將模型回答中的 source_id 引用映射為完整來源資訊。"""

    # 模型回答中引用 source_id 的模式：[source:abc123] 或 [ref:abc123]
    CITATION_PATTERN = re.compile(r"\[(?:source|ref|src):([a-f0-9]{8,16})\]", re.IGNORECASE)

    def resolve(
        self,
        answer: str,
        source_metadata: list[SourceMetadata],
    ) -> CitationResult:
        """解析回答中的引用，替換為編號引用。"""
        # 建立 source_id → metadata 映射
        meta_map: dict[str, SourceMetadata] = {
            m.source_id or "": m for m in source_metadata
        }

        citations: list[Citation] = []
        citation_map: dict[str, Citation] = {}
        unresolved: list[str] = []
        next_id = 1

        def replace_ref(match: re.Match[str]) -> str:
            nonlocal next_id
            sid = match.group(1)
            if sid in citation_map:
                return f"[{citation_map[sid].citation_id}]"
            meta = meta_map.get(sid)
            if meta is None:
                unresolved.append(sid)
                return f"[ref:{sid}]"
            citation = Citation(
                citation_id=next_id,
                source_id=sid,
                url=meta.url,
                title=meta.title,
                domain=meta.domain,
                published_time=meta.published_time,
            )
            citations.append(citation)
            citation_map[sid] = citation
            next_id += 1
            return f"[{citation.citation_id}]"

        resolved_answer = self.CITATION_PATTERN.sub(replace_ref, answer)

        return CitationResult(
            answer_with_citations=resolved_answer,
            citations=citations,
            unresolved_references=unresolved,
        )


class SourceFormatter:
    """來源格式化器 — 產生使用者可讀的引用清單。"""

    def format(self, result: CitationResult) -> str:
        """格式化最終回覆（答案 + 引用清單）。"""
        if not result.citations:
            return result.answer_with_citations

        lines = [result.answer_with_citations, "", "來源："]
        for c in result.citations:
            lines.append(c.format())
        return "\n".join(lines)

    def format_compact(self, result: CitationResult) -> str:
        """簡潔格式（只附主要來源）。"""
        if not result.citations:
            return result.answer_with_citations
        top = result.citations[:3]
        sources = ", ".join(f"[{c.citation_id}] {c.title or c.domain or c.url or ''}" for c in top)
        return f"{result.answer_with_citations}\n\n來源：{sources}"


__all__ = ["Citation", "CitationResult", "CitationResolver", "SourceFormatter"]
