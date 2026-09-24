"""Collaboration result synthesizer.

Merges provider replies into one integrated summary: dedupes statements,
keeps important differences, tags every statement with its source
provider, and appends the comparator output.

An optional ``synthesis_fn`` may be injected — it must be a governed,
registered model-service callable supplied by the caller; the tool never
reaches into another tool's private model internals.  When unavailable
the deterministic fallback runs and raw replies are never lost.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from .external_content import EXTERNAL_CONTENT_CLASS, seal_external_text

SynthesisFn = Callable[[str, list[dict[str, Any]]], Awaitable[str | None]]

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")


def _statements(text: str) -> list[str]:
    return [
        item.strip().strip("-*• \t")
        for item in _SENTENCE_SPLIT.split(str(text or ""))
        if len(item.strip().strip("-*• \t")) >= 4
    ]


class CollaborationResultSynthesizer:
    def __init__(self, synthesis_fn: SynthesisFn | None = None) -> None:
        self._synthesis_fn = synthesis_fn

    async def synthesize(
        self,
        task_id: str,
        original_request: str,
        responses: list[dict[str, Any]],
        comparison: dict[str, Any],
    ) -> dict[str, Any]:
        completed = [
            item
            for item in responses
            if str(item.get("response_status") or "") == "completed"
            and str(item.get("response_text") or "").strip()
        ]
        method = "deterministic"
        llm_text: str | None = None
        if self._synthesis_fn is not None and completed:
            try:
                llm_text = await self._synthesis_fn(original_request, completed)
            except Exception:
                llm_text = None  # governed model unavailable → fallback
            if llm_text:
                method = "governed-model"

        summary_lines = self._deterministic_summary(
            original_request, completed, comparison, llm_text
        )
        return {
            "task_id": task_id,
            "method": method,
            "content_class": EXTERNAL_CONTENT_CLASS,
            "summary": summary_lines,
            "sources": [
                {
                    "provider_id": str(item.get("provider_id") or ""),
                    "response_id": str(item.get("response_id") or ""),
                    "capture_method": str(item.get("capture_method") or ""),
                }
                for item in completed
            ],
            "raw_responses_preserved": True,
            "comparison_reference": f"collab_task:{task_id}:comparison",
        }

    @staticmethod
    def _deterministic_summary(
        original_request: str,
        completed: list[dict[str, Any]],
        comparison: dict[str, Any],
        llm_text: str | None,
    ) -> str:
        """Deterministic arrangement: dedupe, keep differences, tag sources."""
        seen: set[str] = set()
        lines: list[str] = [
            f"協作整合（{len(completed)} 個 AI 回覆；比較僅表示輸出差異，不代表正確性）",
            f"原始需求：{str(original_request or '').strip()[:200]}",
        ]
        for item in completed:
            provider = str(item.get("provider_id") or "")
            lines.append(f"\n【{provider}】")
            for statement in _statements(
                seal_external_text(item.get("response_text"))
            )[:12]:
                key = re.sub(r"\s+", " ", statement.casefold())
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"- {statement}")
        common = comparison.get("common_points") or []
        if common:
            lines.append("\n【共同觀點】")
            for entry in common[:8]:
                lines.append(f"- {entry.get('text')}")
        differences = comparison.get("differences") or []
        if differences:
            lines.append("\n【各 AI 差異】")
            for entry in differences[:12]:
                lines.append(
                    f"- [{entry.get('source_provider')}] {entry.get('text')}"
                )
        contradictions = comparison.get("contradictions") or []
        if contradictions:
            lines.append("\n【相互矛盾】")
            for entry in contradictions[:8]:
                for stmt in entry.get("statements") or []:
                    lines.append(
                        f"- [{stmt.get('provider_id')}] {stmt.get('text')}"
                    )
        unanswered = comparison.get("unanswered_questions") or []
        if unanswered:
            lines.append("\n【尚未回答的問題】")
            for entry in unanswered[:8]:
                lines.append(f"- {entry.get('question')}（{entry.get('raised_by')}）")
        if llm_text:
            lines.append("\n【受管模型彙整】")
            lines.append(llm_text.strip()[:4000])
        return "\n".join(lines)
