"""Evidence Selector — 從 Reranker 結果中選取真正需要的內容。

對應需求 34：
  Ranked Chunks → Evidence Selection → Token Budget Check → Final Evidence Set
  優先保留：最直接回答問題的資料、高可信來源、時間最新資料、
  不同來源互相驗證的內容
  避免把大量低相關內容全部塞進 Context。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chunking import TextChunk
from .reranker import RerankResult


@dataclass
class Evidence:
    """選出的證據片段。"""

    chunk: TextChunk
    score: float
    selected_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "url": self.chunk.url,
            "title": self.chunk.title,
            "score": round(self.score, 4),
            "selected_reason": self.selected_reason,
            "token_estimate": self.chunk.token_estimate,
        }


@dataclass
class EvidenceSelectionResult:
    """證據選取結果。"""

    evidence: list[Evidence] = field(default_factory=list)
    total_tokens: int = 0
    token_budget: int = 0
    skipped: int = 0
    cross_validated: list[dict[str, Any]] = field(default_factory=list)


class EvidenceSelector:
    """證據選取器。"""

    def __init__(
        self,
        token_budget: int = 2048,
        min_score: float = 0.1,
        max_evidence: int = 8,
        prefer_cross_validated: bool = True,
    ) -> None:
        self.token_budget = token_budget
        self.min_score = min_score
        self.max_evidence = max_evidence
        self.prefer_cross_validated = prefer_cross_validated

    def select(
        self,
        ranked: list[RerankResult],
        *,
        token_budget: int | None = None,
        cross_validation: list[dict[str, Any]] | None = None,
    ) -> EvidenceSelectionResult:
        budget = token_budget or self.token_budget
        result = EvidenceSelectionResult(token_budget=budget)

        # 建立交叉驗證 map：chunk_id → 被多少其他 chunk 驗證
        cv_count: dict[str, int] = {}
        if cross_validation and self.prefer_cross_validated:
            for cv in cross_validation:
                for key in ("primary", "secondary"):
                    cid = cv.get(key)
                    if cid:
                        cv_count[cid] = cv_count.get(cid, 0) + 1

        selected_ids: set[str] = set()
        for item in ranked:
            if len(result.evidence) >= self.max_evidence:
                break
            if item.score < self.min_score:
                result.skipped += 1
                continue
            if item.chunk.chunk_id in selected_ids:
                continue

            # Token budget 檢查
            if result.total_tokens + item.chunk.token_estimate > budget:
                # 嘗試截斷最後一個 evidence
                remaining = budget - result.total_tokens
                if remaining < 50:
                    result.skipped += 1
                    continue
                # 截斷文字
                truncated_text = item.chunk.text[:remaining * 4]  # 粗略
                item.chunk.text = truncated_text
                item.chunk.token_estimate = remaining

            reason = "high_score"
            cv = cv_count.get(item.chunk.chunk_id, 0)
            if cv > 0:
                reason = f"high_score+cross_validated({cv})"

            result.evidence.append(
                Evidence(chunk=item.chunk, score=item.score, selected_reason=reason)
            )
            result.total_tokens += item.chunk.token_estimate
            selected_ids.add(item.chunk.chunk_id)

        # 記錄交叉驗證
        if cross_validation:
            for cv in cross_validation:
                if cv.get("primary") in selected_ids:
                    result.cross_validated.append(cv)

        return result


__all__ = ["EvidenceSelector", "Evidence", "EvidenceSelectionResult"]
