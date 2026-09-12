"""Context Builder — 建立最終送入星澄模型的 Context。

對應需求 10, 35：
  Context 組合順序依任務動態決定：
    System / Governance → 使用者問題 → 本地相關資料 → SQL 結果
    → Local RAG → Web Evidence → Source Metadata → 任務指令
  每段 Web Evidence 必須保留 Source ID。
  模型看到的是整理過的證據，不是原始搜尋頁面。
  來源至少區分：Local RAG / SQL / Web Search / Model Knowledge。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..source import SourceMetadata, SourceType
from ..chunking import TextChunk
from ..evidence_selector import Evidence


@dataclass
class ContextEntry:
    """Context 中的單一項目。"""

    source_type: SourceType
    content: str
    source_id: str | None = None
    title: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "content_length": len(self.content),
        }


@dataclass
class BuiltContext:
    """建構完成的 Context。"""

    entries: list[ContextEntry] = field(default_factory=list)
    full_text: str = ""
    token_estimate: int = 0
    source_count: int = 0
    source_breakdown: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_count": len(self.entries),
            "full_text_length": len(self.full_text),
            "token_estimate": self.token_estimate,
            "source_count": self.source_count,
            "source_breakdown": self.source_breakdown,
            "entries": [e.to_dict() for e in self.entries],
        }


class ContextBuilder:
    """Context 建構器。"""

    def __init__(
        self,
        system_prompt: str = "",
        governance_rules: str = "",
        max_tokens: int = 4096,
    ) -> None:
        self.system_prompt = system_prompt
        self.governance_rules = governance_rules
        self.max_tokens = max_tokens

    def build(
        self,
        *,
        user_question: str,
        sql_results: list[ContextEntry] | None = None,
        local_rag_chunks: list[TextChunk] | None = None,
        web_evidence: list[Evidence] | None = None,
        model_knowledge: str | None = None,
        task_instruction: str = "",
        source_metadata: list[SourceMetadata] | None = None,
    ) -> BuiltContext:
        """依動態順序組合 Context。"""
        entries: list[ContextEntry] = []

        # 1. System / Governance
        if self.system_prompt:
            entries.append(ContextEntry(
                source_type=SourceType.SYSTEM_RULE,
                content=self.system_prompt,
                title="System Prompt",
            ))
        if self.governance_rules:
            entries.append(ContextEntry(
                source_type=SourceType.SYSTEM_RULE,
                content=self.governance_rules,
                title="Governance Rules",
            ))

        # 2. 使用者問題
        entries.append(ContextEntry(
            source_type=SourceType.MODEL_KNOWLEDGE,
            content=f"使用者問題：{user_question}",
            title="User Question",
        ))

        # 3. 本地相關資料 / SQL 結果
        if sql_results:
            for entry in sql_results:
                entries.append(entry)

        # 4. Local RAG
        if local_rag_chunks:
            for chunk in local_rag_chunks:
                entries.append(ContextEntry(
                    source_type=SourceType.LOCAL_RAG,
                    content=chunk.text,
                    source_id=chunk.document_id,
                    title=chunk.title,
                    url=chunk.url,
                    metadata={"position": chunk.position, "published_time": chunk.published_time},
                ))

        # 5. Web Evidence（每段保留 Source ID）
        if web_evidence:
            for ev in web_evidence:
                chunk = ev.chunk
                entries.append(ContextEntry(
                    source_type=SourceType.WEB_SEARCH,
                    content=chunk.text,
                    source_id=chunk.chunk_id,
                    title=chunk.title,
                    url=chunk.url,
                    metadata={
                        "score": ev.score,
                        "domain": chunk.domain,
                        "published_time": chunk.published_time,
                        "fetched_time": chunk.fetched_time,
                        "selected_reason": ev.selected_reason,
                    },
                ))

        # 6. Model Knowledge
        if model_knowledge:
            entries.append(ContextEntry(
                source_type=SourceType.MODEL_KNOWLEDGE,
                content=model_knowledge,
                title="Model Knowledge",
            ))

        # 7. Source Metadata 摘要
        if source_metadata:
            meta_text = self._format_source_metadata(source_metadata)
            entries.append(ContextEntry(
                source_type=SourceType.SYSTEM_RULE,
                content=meta_text,
                title="Source Metadata",
            ))

        # 8. 任務指令
        if task_instruction:
            entries.append(ContextEntry(
                source_type=SourceType.SYSTEM_RULE,
                content=task_instruction,
                title="Task Instruction",
            ))

        # 組合全文 + token 預算控制
        full_text, token_est, final_entries = self._assemble(entries)

        breakdown: dict[str, int] = {}
        for e in final_entries:
            breakdown[e.source_type.value] = breakdown.get(e.source_type.value, 0) + 1

        return BuiltContext(
            entries=final_entries,
            full_text=full_text,
            token_estimate=token_est,
            source_count=len({e.source_id for e in final_entries if e.source_id}),
            source_breakdown=breakdown,
        )

    def _assemble(self, entries: list[ContextEntry]) -> tuple[str, int, list[ContextEntry]]:
        """組合全文，控制 token 預算。"""
        parts: list[str] = []
        final_entries: list[ContextEntry] = []
        total_tokens = 0

        for entry in entries:
            tokens = self._estimate_tokens(entry.content)
            if total_tokens + tokens > self.max_tokens:
                # 截斷
                remaining = self.max_tokens - total_tokens
                if remaining < 20:
                    break
                entry.content = entry.content[: remaining * 4]
                tokens = remaining
            parts.append(self._format_entry(entry))
            final_entries.append(entry)
            total_tokens += tokens

        return "\n\n".join(parts), total_tokens, final_entries

    @staticmethod
    def _format_entry(entry: ContextEntry) -> str:
        header = f"[{entry.source_type.value}"
        if entry.title:
            header += f": {entry.title}"
        if entry.url:
            header += f" | {entry.url}"
        if entry.source_id:
            header += f" | source_id={entry.source_id}"
        header += "]"
        return f"{header}\n{entry.content}"

    @staticmethod
    def _format_source_metadata(metadata: list[SourceMetadata]) -> str:
        lines: list[str] = ["來源清單："]
        for m in metadata:
            line = f"- [{m.source_type.value}] "
            if m.title:
                line += m.title
            if m.url:
                line += f" ({m.url})"
            if m.published_time:
                line += f" | 發布: {m.published_time}"
            if m.relevance_score:
                line += f" | 相關度: {m.relevance_score:.2f}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        chinese_chars = sum(1 for c in text if "\u3400" <= c <= "\u9fff")
        other_chars = len(text) - chinese_chars
        return chinese_chars + other_chars // 4


__all__ = ["ContextBuilder", "ContextEntry", "BuiltContext"]
