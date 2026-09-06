"""Intent Router — 搜尋意圖判斷。

對應需求 1, 12, 21：
  由星澄判斷目前問題是否需要即時網路資料。
  需要即時資訊時才啟動網路搜尋。
  不需要即時資料時優先使用：模型自身知識 → 本地 SQL → 本地 RAG → 本地向量資料庫
  時效性問題類型：最新消息、新聞、價格、版本、軟體更新、政策、法規、
  市場資訊、產品規格、人物現況、公司資訊、服務狀態、近期事件
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any


class SearchIntent(enum.Enum):
    """搜尋意圖類型。"""

    NO_SEARCH_NEEDED = "no_search_needed"       # 模型知識足夠
    LOCAL_DATA_PREFERRED = "local_data_preferred"  # 優先本地 SQL / RAG
    WEB_SEARCH_RECOMMENDED = "web_search_recommended"  # 建議網路搜尋
    WEB_SEARCH_REQUIRED = "web_search_required"  # 必須網路搜尋（時效性）


# 時效性關鍵詞（需求 12）
_TIME_SENSITIVE_KEYWORDS = (
    # 中文
    "最新", "現在", "目前", "今天", "本週", "本月", "今年", "即時",
    "最新消息", "新聞", "價格", "報價", "行情", "版本", "軟體更新",
    "政策", "法規", "市場", "產品規格", "人物", "公司", "服務狀態",
    "近期", "最近", "即將", "未來", "明天", "昨天",
    # 英文
    "latest", "current", "today", "now", "real-time", "recent", "breaking",
    "news", "price", "version", "update", "release", "changelog",
    "policy", "regulation", "market", "status", "outage",
)

# 明確需要本地資料的關鍵詞
_LOCAL_DATA_KEYWORDS = (
    "我的", "本地", "本機", "資料庫", "持股", "投資組合",
    "my", "local", "database", "portfolio", "holdings",
)

# 歷史 / 穩定知識關鍵詞（不需要網路搜尋）
_STABLE_KNOWLEDGE_KEYWORDS = (
    "歷史", "原理", "定義", "什麼是", "如何運作", "解釋",
    "history", "principle", "definition", "what is", "how does",
    "explain", "tutorial", "概念",
)


@dataclass
class IntentResult:
    """意圖判斷結果。"""

    intent: SearchIntent
    confidence: float = 0.5
    reasons: list[str] = field(default_factory=list)
    suggested_tools: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_web_search(self) -> bool:
        return self.intent in (SearchIntent.WEB_SEARCH_RECOMMENDED, SearchIntent.WEB_SEARCH_REQUIRED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
            "suggested_tools": self.suggested_tools,
            "needs_web_search": self.needs_web_search,
        }


class IntentRouter:
    """搜尋意圖路由器。"""

    def __init__(
        self,
        time_sensitive_keywords: tuple[str, ...] = _TIME_SENSITIVE_KEYWORDS,
        local_keywords: tuple[str, ...] = _LOCAL_DATA_KEYWORDS,
        stable_keywords: tuple[str, ...] = _STABLE_KNOWLEDGE_KEYWORDS,
    ) -> None:
        self.time_sensitive = time_sensitive_keywords
        self.local_keywords = local_keywords
        self.stable_keywords = stable_keywords

    def classify(self, question: str) -> IntentResult:
        """判斷使用者問題的搜尋意圖。"""
        lower = question.lower()
        reasons: list[str] = []
        score = 0.0

        # 時效性關鍵詞
        time_hits = [kw for kw in self.time_sensitive if kw in lower or kw in question]
        if time_hits:
            score += 0.4 * min(1.0, len(time_hits) / 3)
            reasons.append(f"時效性關鍵詞: {', '.join(time_hits[:3])}")

        # 本地資料關鍵詞
        local_hits = [kw for kw in self.local_keywords if kw in lower or kw in question]
        if local_hits:
            score -= 0.2
            reasons.append(f"本地資料關鍵詞: {', '.join(local_hits[:3])}")

        # 穩定知識關鍵詞
        stable_hits = [kw for kw in self.stable_keywords if kw in lower or kw in question]
        if stable_hits:
            score -= 0.3
            reasons.append(f"穩定知識關鍵詞: {', '.join(stable_hits[:3])}")

        # 問號 / 疑問詞
        if "?" in question or "？" in question:
            score += 0.05

        # 決策
        if score >= 0.35:
            intent = SearchIntent.WEB_SEARCH_REQUIRED
            suggested = ["web_search", "web_fetch", "reranker", "context_builder"]
        elif score >= 0.15:
            intent = SearchIntent.WEB_SEARCH_RECOMMENDED
            suggested = ["local_rag", "web_search", "reranker", "context_builder"]
        elif score <= -0.2:
            intent = SearchIntent.NO_SEARCH_NEEDED
            suggested = ["model_knowledge"]
        else:
            intent = SearchIntent.LOCAL_DATA_PREFERRED
            suggested = ["local_rag", "sql"]

        confidence = min(1.0, abs(score) + 0.3)

        return IntentResult(
            intent=intent,
            confidence=confidence,
            reasons=reasons,
            suggested_tools=suggested,
        )


__all__ = ["IntentRouter", "IntentResult", "SearchIntent"]
