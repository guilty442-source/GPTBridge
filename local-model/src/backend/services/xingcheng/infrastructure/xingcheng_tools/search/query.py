"""Query Planner / Query Builder — 搜尋查詢產生與策略規劃。

對應需求 3, 23：
  - 將使用者問題轉換為一個或多個搜尋 Query
  - 支援中文查詢、英文查詢、關鍵字擴展、同義詞、多查詢搜尋、
    時間條件、網站限制、結果數限制
  - Query Planner：問題拆解 → 關鍵字抽取 → Query Rewrite → 多組 Query
  - 支援：單一 Query、多 Query、中文＋英文交叉搜尋、時間敏感 Query、
    指定網站 Query、實體名稱精確搜尋、版本號搜尋、錯字／別名展開
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .types import SearchRequest


# ── 同義詞 / 別名展開（可擴充）──────────────────────────────────
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "最新": ("latest", "newest", "recent"),
    "價格": ("price", "pricing", "報價", "行情"),
    "版本": ("version", "release", "changelog"),
    "更新": ("update", "upgrade", "patch"),
    "新聞": ("news", "報導", "報道"),
    "文件": ("documentation", "docs", "文檔"),
    "下載": ("download", "下載點"),
    "教學": ("tutorial", "guide", "指南"),
    "比較": ("compare", "comparison", "vs", "對比"),
    "評價": ("review", "評測", "測試"),
}

# 時間敏感關鍵詞
_TIME_SENSITIVE_KEYWORDS = (
    "最新", "現在", "目前", "今天", "本週", "本月", "今年",
    "即時", "real-time", "current", "today", "this week", "this month",
    "now", "latest", "recent", "breaking",
)

# 實體名稱模式（大寫開頭英文 / 中文專有名詞）
_ENTITY_PATTERN = re.compile(
    r"[A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*|"
    r"[\u3400-\u9fff]{2,8}(?:[\u3400-\u9fff]{1,4})?"
)

# 版本號模式
_VERSION_PATTERN = re.compile(
    r"v?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?", re.IGNORECASE
)


@dataclass
class QueryPlan:
    """Query Planner 產出的搜尋策略。"""

    queries: list[str] = field(default_factory=list)
    is_time_sensitive: bool = False
    entities: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    strategy: str = "single"  # single / multi / cross_lang / site_specific
    extra: dict[str, Any] = field(default_factory=dict)


class QueryBuilder:
    """搜尋查詢產生器。"""

    def __init__(self, synonyms: dict[str, tuple[str, ...]] | None = None) -> None:
        self.synonyms = synonyms or _SYNONYMS

    def build_request(
        self,
        question: str,
        *,
        language: str = "zh-TW",
        max_results: int = 10,
        time_range: str | None = None,
        site_restrictions: list[str] | None = None,
        need_full_text: bool = True,
    ) -> SearchRequest:
        """從使用者問題建立完整 SearchRequest（含 Query Plan）。"""
        plan = self.plan(question)
        return SearchRequest(
            original_question=question,
            queries=plan.queries,
            language=language,
            time_range=time_range or ("pastweek" if plan.is_time_sensitive else None),
            source_restrictions=site_restrictions or [],
            max_results=max_results,
            need_full_text=need_full_text,
            extra={
                "query_plan": {
                    "is_time_sensitive": plan.is_time_sensitive,
                    "entities": plan.entities,
                    "versions": plan.versions,
                    "keywords": plan.keywords,
                    "strategy": plan.strategy,
                },
            },
        )

    def plan(self, question: str) -> QueryPlan:
        """產生搜尋策略。"""
        question = question.strip()
        if not question:
            return QueryPlan()

        # 1. 關鍵字抽取
        keywords = self._extract_keywords(question)
        # 2. 實體名稱
        entities = self._extract_entities(question)
        # 3. 版本號
        versions = _VERSION_PATTERN.findall(question)
        # 4. 時間敏感判斷
        is_time_sensitive = self._is_time_sensitive(question)
        # 5. 同義詞擴展
        expanded = self._expand_synonyms(keywords)
        # 6. Query 產生
        queries = self._build_queries(
            question, keywords, entities, expanded, is_time_sensitive
        )

        strategy = "single"
        if len(queries) > 1:
            strategy = "multi"
        if any(re.search(r"[a-zA-Z]", q) and re.search(r"[\u3400-\u9fff]", q) for q in queries):
            strategy = "cross_lang"

        return QueryPlan(
            queries=queries[:8],  # 限制最多 8 組 Query
            is_time_sensitive=is_time_sensitive,
            entities=entities,
            versions=versions,
            keywords=keywords,
            strategy=strategy,
        )

    # ── 內部方法 ────────────────────────────────────────────────
    def _extract_keywords(self, text: str) -> list[str]:
        # 移除常見停用詞
        stop = {"的", "是", "在", "了", "嗎", "呢", "吧", "請", "幫", "我", "你", "他", "她"}
        # 中文 bi-gram + 英文單詞
        tokens: list[str] = []
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]+", text):
            if len(word) > 1 and word.lower() not in stop:
                tokens.append(word)
        for run in re.findall(r"[\u3400-\u9fff]+", text):
            for i in range(len(run) - 1):
                bigram = run[i : i + 2]
                if bigram not in stop:
                    tokens.append(bigram)
        # 去重保序
        seen: set[str] = set()
        result: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    def _extract_entities(self, text: str) -> list[str]:
        return list(dict.fromkeys(_ENTITY_PATTERN.findall(text)))[:5]

    def _is_time_sensitive(self, text: str) -> bool:
        lower = text.lower()
        for kw in _TIME_SENSITIVE_KEYWORDS:
            if kw in lower or kw in text:
                return True
        return False

    def _expand_synonyms(self, keywords: list[str]) -> dict[str, list[str]]:
        expanded: dict[str, list[str]] = {}
        for kw in keywords:
            for syn_key, syn_vals in self.synonyms.items():
                if kw == syn_key or kw in syn_vals:
                    expanded[kw] = [v for v in syn_vals if v != kw]
                    break
        return expanded

    def _build_queries(
        self,
        question: str,
        keywords: list[str],
        entities: list[str],
        expanded: dict[str, list[str]],
        is_time_sensitive: bool,
    ) -> list[str]:
        queries: list[str] = []

        # 主 Query：原始問題（精簡後）
        main_q = question.strip()
        queries.append(main_q)

        # 關鍵字 Query
        if keywords:
            kw_q = " ".join(keywords[:6])
            if kw_q != main_q:
                queries.append(kw_q)

        # 實體精確搜尋
        for entity in entities[:2]:
            if entity not in main_q:
                continue
            queries.append(f'"{entity}"')

        # 同義詞擴展 Query
        for kw, syns in expanded.items():
            for syn in syns[:2]:
                expanded_q = main_q.replace(kw, syn)
                if expanded_q not in queries:
                    queries.append(expanded_q)

        # 時間敏感：加日期修飾
        if is_time_sensitive:
            import datetime
            year = datetime.date.today().year
            queries.append(f"{main_q} {year}")

        # 中英交叉：如果有中文且有英文關鍵字
        has_chinese = bool(re.search(r"[\u3400-\u9fff]", main_q))
        english_kws = [k for k in keywords if re.match(r"[A-Za-z]", k)]
        if has_chinese and english_kws:
            queries.append(f"{main_q} {' '.join(english_kws[:3])}")

        return queries


__all__ = ["QueryBuilder", "QueryPlan"]
