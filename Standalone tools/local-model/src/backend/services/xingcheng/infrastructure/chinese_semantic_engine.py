"""Chinese Semantic Engine — 星澄繁體中文（台灣）語意引擎。

Deterministic Traditional-Chinese semantic analysis for Xingcheng.  The
engine consolidates the declared understanding features into one cohesive,
side-effect-free module:

  * original-text preservation with NFKC/width normalization,
  * Taiwan-Chinese synonym normalization and colloquial/typo repair,
  * active-verb and operation-object detection,
  * parameter and specific-constraint extraction,
  * conversation-context (ellipsis/reference) completion,
  * destructive-ambiguity safety confirmation,
  * multi-intent planning, question-type and negation detection,
  * requested-output detection and keyword extraction,
  * date/percentage/money/URL/email/version/path/model entities,
  * mixed-language detection and language priority.

The result carries the ``star-chinese-semantics/v1`` schema and can be
projected to the ``star-semantic-plan/v1`` plan shape used by the governed
inference pipeline.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA = "star-chinese-semantics/v1"

UNDERSTANDING_FEATURES: tuple[str, ...] = (
    "original-traditional-chinese-preservation",
    "taiwan-chinese-synonym-normalization",
    "colloquial-ellipsis-typo-and-mixed-language-input",
    "active-verb-and-operation-object-detection",
    "parameter-and-specific-constraint-extraction",
    "conversation-context-completion",
    "destructive-ambiguity-safety-confirmation",
    "multi-intent-planning",
    "question-type-detection",
    "negation-detection",
    "constraint-extraction",
    "requested-output-detection",
    "keyword-extraction",
    "date-percentage-money-url-email-entities",
    "mixed-language-detection",
)

#: Mainland-Chinese terms → Taiwan-Chinese canonical terms.
TAIWAN_SYNONYMS: Mapping[str, str] = {
    "軟件": "軟體",
    "硬件": "硬體",
    "網絡": "網路",
    "互联网": "網際網路",
    "信息": "資訊",
    "視頻": "影片",
    "音頻": "音訊",
    "硬盤": "硬碟",
    "內存": "記憶體",
    "緩存": "快取",
    "激光": "雷射",
    "打印": "列印",
    "打印機": "印表機",
    "默認": "預設",
    "配置文件": "設定檔",
    "鼠標": "滑鼠",
    "屏幕": "螢幕",
    "顯示屏": "顯示器",
    "服務器": "伺服器",
    "移動電話": "行動電話",
    "出租車": "計程車",
    "自行車": "腳踏車",
    "質量": "品質",
    "項目": "專案",
    "文檔": "文件",
    "菜單": "選單",
    "芯片": "晶片",
    "智能": "智慧",
    "算法": "演算法",
    "數據庫": "資料庫",
    "數據": "資料",
    "日誌": "紀錄",
    "登錄": "登入",
    "退出": "登出",
    "卸載": "解除安裝",
    "加載": "載入",
    "保存": "儲存",
    "剪切": "剪下",
    "粘貼": "貼上",
    "刷新": "重新整理",
    "字節": "位元組",
    "代碼": "程式碼",
}

#: Colloquial spellings, common typos and mixed-script repairs.  Only
#: unambiguous multi-character forms are listed; single characters that also
#: occur inside normal words (醬油, 粉絲, 偶像) are deliberately excluded.
COLLOQUIAL_REPAIRS: Mapping[str, str] = {
    "甚麼": "什麼",
    "那裏": "那裡",
    "裏面": "裡面",
    "臺灣": "台灣",
    "醬子": "這樣子",
    "口以": "可以",
    "粗乃": "出來",
    "ㄉ": "的",
    "ㄌ": "了",
    "ㄅ": "不",
    "ㄏ": "很",
    "ㄇ": "嗎",
}

INTENT_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("coding", ("程式", "程式碼", "代碼", "函式", "函數", "腳本", "重構", "編譯", "測試", "python", "javascript", "typescript", "修 bug", "修復程式")),
    ("search", ("搜尋", "查詢", "找一下", "幫我找", "上網", "檢索", "查資料", "search")),
    ("analysis", ("分析", "比較", "評估", "判斷", "推論", "統計", "趨勢", "風險")),
    ("status", ("狀態", "健康", "目前", "現在", "進度", "模型", "版本", "設定", "組態")),
    ("reading", ("閱讀", "讀取", "摘要這份", "這份文件", "這篇文章", "文件內容")),
    ("visual", ("圖片", "照片", "影像", "影片", "截圖", "圖表", "ocr")),
    ("data_organization", ("整理", "歸類", "分類", "排序", "清單", "表格", "資料整理")),
    ("calculation", ("計算", "算一下", "加總", "平均", "百分比", "公式")),
    ("investment", ("股票", "台股", "美股", "投資", "持股", "股利", "行情", "股價", "基金")),
    ("memory", ("記憶", "記住", "回想", "之前說過", "上次提到")),
    ("training", ("訓練", "微調", "學習", "模型訓練", "資料集")),
    ("repair", ("修復", "維修", "排除", "故障", "錯誤", "異常", "壞掉")),
    ("self_upgrade", ("自我升級", "自我更新", "升級自己", "更新模型")),
    ("command_execution", ("執行", "跑一下", "啟動", "停止", "關閉", "開啟", "部署")),
    ("conversation", ("你好", "您好", "哈囉", "嗨", "聊聊", "謝謝", "請問你是")),
)

#: Destructive actions that require unambiguous intent.
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset(
    {"delete", "remove", "overwrite", "format", "reset", "terminate", "drop"}
)

#: Governance surfaces that a request may never mutate.
PROHIBITED_SURFACES: tuple[str, ...] = (
    "法典",
    "治理規則",
    "權限設定",
    "governance_rule",
    "permission_directory",
    "法典資料庫",
)

ACTION_LEXICON: tuple[tuple[str, str], ...] = (
    ("建立", "create"),
    ("新增", "create"),
    ("產生", "generate"),
    ("生成", "generate"),
    ("撰寫", "generate"),
    ("修改", "modify"),
    ("調整", "modify"),
    ("更新", "update"),
    ("刪除", "delete"),
    ("移除", "remove"),
    ("清空", "delete"),
    ("覆蓋", "overwrite"),
    ("格式化", "format"),
    ("重設", "reset"),
    ("重置", "reset"),
    ("移動", "move"),
    ("搬移", "move"),
    ("複製", "copy"),
    ("重新命名", "rename"),
    ("改名", "rename"),
    ("執行", "execute"),
    ("啟動", "execute"),
    ("停止", "stop"),
    ("終止", "terminate"),
    ("開啟", "open"),
    ("關閉", "close"),
    ("查詢", "query"),
    ("搜尋", "search"),
    ("讀取", "read"),
    ("寫入", "write"),
    ("匯出", "export"),
    ("匯入", "import"),
    ("備份", "backup"),
    ("還原", "restore"),
    ("排程", "schedule"),
    ("分析", "analyze"),
    ("整理", "organize"),
    ("摘要", "summarize"),
    ("翻譯", "translate"),
)

OBJECT_LEXICON: tuple[tuple[str, str], ...] = (
    ("資料庫", "database"),
    ("資料表", "database-table"),
    ("檔案", "file"),
    ("文件", "file"),
    ("資料夾", "folder"),
    ("目錄", "folder"),
    ("程式碼", "code"),
    ("程式", "code"),
    ("腳本", "script"),
    ("報告", "report"),
    ("報表", "report"),
    ("圖表", "chart"),
    ("設定", "settings"),
    ("組態", "settings"),
    ("模型", "model"),
    ("權重", "model-weights"),
    ("記憶", "memory"),
    ("任務", "task"),
    ("工具", "tool"),
    ("網頁", "web-page"),
    ("圖片", "image"),
    ("影片", "video"),
    ("備份", "backup"),
    ("日誌", "log"),
    ("紀錄", "log"),
    ("郵件", "email"),
    ("帳號", "account"),
    ("排程", "schedule"),
    ("服務", "service"),
    ("系統", "system"),
)

NEGATION_MARKERS: tuple[str, ...] = (
    "不要",
    "不用",
    "不需要",
    "不必",
    "別",
    "勿",
    "禁止",
    "不可以",
    "不准",
    "不得",
    "無需",
    "毋須",
    "no",
    "don't",
    "do not",
)

QUESTION_MARKERS: tuple[tuple[str, str], ...] = (
    ("嗎", "yes-no"),
    ("呢", "follow-up"),
    ("是否", "yes-no"),
    ("可否", "permission"),
    ("能不能", "capability"),
    ("可不可以", "permission"),
    ("什麼", "what"),
    ("甚麼", "what"),
    ("哪些", "which"),
    ("哪個", "which"),
    ("哪裡", "where"),
    ("何時", "when"),
    ("什麼時候", "when"),
    ("為什麼", "why"),
    ("為何", "why"),
    ("如何", "how"),
    ("怎麼", "how"),
    ("誰", "who"),
    ("多少", "how-many"),
    ("幾個", "how-many"),
)

CONSTRAINT_MARKERS: tuple[str, ...] = (
    "至少",
    "最多",
    "不超過",
    "超過",
    "限定",
    "只能",
    "只可以",
    "必須",
    "一定要",
    "範圍",
    "格式",
    "字數",
    "字以內",
    "分鐘內",
    "小時內",
    "天內",
    "以內",
    "以外",
)

REQUESTED_OUTPUT_MARKERS: tuple[tuple[str, str], ...] = (
    ("表格", "table"),
    ("清單", "list"),
    ("列表", "list"),
    ("摘要", "summary"),
    ("報告", "report"),
    ("json", "json"),
    ("csv", "csv"),
    ("程式碼", "code"),
    ("步驟", "steps"),
    ("建議", "recommendation"),
    ("比較", "comparison"),
    ("分析", "analysis"),
    ("結論", "conclusion"),
)

REFERENCE_MARKERS: tuple[str, ...] = (
    "它",
    "他",
    "她",
    "這個",
    "那個",
    "上述",
    "上面",
    "剛剛",
    "前面",
    "同樣",
    "再",
    "也",
    "那份",
    "這份",
)

MULTI_INTENT_MARKERS: tuple[str, ...] = ("並且", "然後", "以及", "同時", "還有", "接著", "再來", "並")

MARKET_ALIASES: Mapping[str, str] = {
    "台股": "TW",
    "臺股": "TW",
    "美股": "US",
    "日股": "JP",
    "港股": "HK",
    "陸股": "CN",
    "歐股": "EU",
}

STOPWORDS: frozenset[str] = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "我們",
        "你們",
        "他們",
        "這",
        "那",
        "個",
        "請",
        "幫",
        "把",
        "給",
        "跟",
        "和",
        "與",
        "或",
        "就",
        "都",
        "很",
        "也",
        "還",
        "要",
        "有",
        "沒有",
        "可以",
        "一下",
        "嗎",
        "呢",
        "吧",
        "啊",
        "喔",
        "哦",
    }
)


def _digest_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _fullwidth_to_halfwidth(text: str) -> str:
    chars: list[str] = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(char)
    return "".join(chars)


@dataclass(frozen=True, slots=True)
class ChineseSemanticAnalysis:
    """Closed semantic analysis result for one Traditional-Chinese input."""

    schema: str
    raw_text: str
    normalized_text: str
    language: str
    language_priority: tuple[str, ...]
    token_count: int
    tokens: tuple[str, ...]
    keywords: tuple[str, ...]
    keyword_counts: tuple[tuple[str, int], ...]
    synonyms: tuple[tuple[str, str], ...]
    repairs: tuple[tuple[str, str], ...]
    intents: tuple[str, ...]
    primary_intent: str
    matched_terms: Mapping[str, tuple[str, ...]]
    prohibited_intents: tuple[str, ...]
    actions: tuple[Mapping[str, Any], ...]
    operation_objects: tuple[Mapping[str, Any], ...]
    entities: Mapping[str, tuple[str, ...]]
    negations: tuple[Mapping[str, Any], ...]
    questions: tuple[str, ...]
    constraints: tuple[str, ...]
    requested_outputs: tuple[str, ...]
    ellipsis: Mapping[str, Any]
    mixed_language: Mapping[str, Any]
    destructive: bool
    confirmation_required: bool
    safety: Mapping[str, Any]
    complexity: Mapping[str, Any]
    features: tuple[str, ...] = UNDERSTANDING_FEATURES
    digest: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "language": self.language,
            "language_priority": list(self.language_priority),
            "token_count": self.token_count,
            "tokens": list(self.tokens),
            "keywords": list(self.keywords),
            "keyword_counts": [list(item) for item in self.keyword_counts],
            "synonyms": [list(item) for item in self.synonyms],
            "repairs": [list(item) for item in self.repairs],
            "intents": list(self.intents),
            "primary_intent": self.primary_intent,
            "matched_terms": {k: list(v) for k, v in self.matched_terms.items()},
            "prohibited_intents": list(self.prohibited_intents),
            "actions": [dict(item) for item in self.actions],
            "operation_objects": [dict(item) for item in self.operation_objects],
            "entities": {k: list(v) for k, v in self.entities.items()},
            "negations": [dict(item) for item in self.negations],
            "questions": list(self.questions),
            "constraints": list(self.constraints),
            "requested_outputs": list(self.requested_outputs),
            "ellipsis": dict(self.ellipsis),
            "mixed_language": dict(self.mixed_language),
            "destructive": self.destructive,
            "confirmation_required": self.confirmation_required,
            "safety": dict(self.safety),
            "complexity": dict(self.complexity),
            "features": list(self.features),
            "digest": self.digest,
        }

    def to_semantic_plan(self) -> dict[str, Any]:
        """Project to the ``star-semantic-plan/v1`` shape used by inference."""
        return {
            "schema": "star-semantic-plan/v1",
            "raw_input": self.raw_text,
            "normalized_input": self.normalized_text,
            "language": self.language,
            "language_priority": list(self.language_priority),
            "intents": list(self.intents),
            "primary_intent": self.primary_intent,
            "matched_terms": {k: list(v) for k, v in self.matched_terms.items()},
            "prohibited_intents": list(self.prohibited_intents),
            "actions": [dict(item) for item in self.actions],
            "operation_objects": [dict(item) for item in self.operation_objects],
            "entities": {k: list(v) for k, v in self.entities.items()},
            "parameters": {
                "model_names": list(self.entities.get("model_names", ())),
                "paths": list(self.entities.get("paths", ())),
                "filenames": list(self.entities.get("filenames", ())),
                "dates": list(self.entities.get("dates", ())),
                "numbers": list(self.entities.get("numbers", ())),
                "versions": list(self.entities.get("versions", ())),
                "output_formats": list(self.requested_outputs),
                "quantization_formats": list(self.entities.get("quantization", ())),
            },
            "specific_constraints": list(self.constraints),
            "negations": [dict(item) for item in self.negations],
            "questions": list(self.questions),
            "requested_outputs": list(self.requested_outputs),
            "context_completion": dict(self.ellipsis),
            "safety": dict(self.safety),
            "comprehension": {
                "keywords": [{"term": term, "count": count} for term, count in self.keyword_counts],
                "negations": [dict(item) for item in self.negations],
                "questions": list(self.questions),
                "complexity": dict(self.complexity),
                "objectives": [self.raw_text.strip()] if self.raw_text.strip() else [],
                "constraints": list(self.constraints),
                "sentence_count": int(self.complexity.get("sentence_count", 0)),
                "requested_outputs": list(self.requested_outputs),
            },
            "task_intensity": self.task_intensity(),
            "task_classification": self.task_classification(),
            "understanding_layer": "traditional-chinese-taiwan-first",
            "engine": {"schema": self.schema, "digest": self.digest},
        }

    def task_intensity(self) -> dict[str, Any]:
        reasons: list[str] = []
        score = 0
        if len(self.actions) > 1:
            score += 2
            reasons.append("multiple-actions")
        if len(self.intents) > 1:
            score += 2
            reasons.append("multiple-intents")
        if len(self.constraints) >= 2:
            score += 2
            reasons.append("multiple-constraints")
        if self.complexity.get("multi_sentence"):
            score += 1
            reasons.append("multi-sentence")
        if self.token_count >= 80:
            score += 2
            reasons.append("long-input")
        elif self.token_count >= 30:
            score += 1
            reasons.append("medium-input")
        if self.intents and self.intents[0] in {
            "coding",
            "analysis",
            "investment",
            "self_upgrade",
            "training",
            "repair",
        }:
            score += 2
            reasons.append("high-reasoning-intent")
        if self.destructive:
            score += 1
            reasons.append("destructive-operation")
        level = (
            "difficult"
            if score >= 5
            else "intermediate"
            if score >= 3
            else "normal"
            if score >= 1
            else "simple"
        )
        return {"level": level, "score": score, "reasons": reasons}

    def task_classification(self) -> dict[str, Any]:
        capability_needs = {
            "agent": bool(self.actions)
            and any(item.get("action") not in {"query", "search", "read"} for item in self.actions),
            "coding": "coding" in self.intents,
            "visual": "visual" in self.intents,
            "multi_model": len(self.intents) > 1,
            "rag_or_search": bool({"search", "reading", "analysis"} & set(self.intents)),
        }
        return {
            "primary": (
                "general_chinese_task"
                if self.primary_intent == "conversation"
                else f"{self.primary_intent}_task"
            ),
            "categories": [f"{intent}_task" for intent in self.intents],
            "capability_needs": capability_needs,
        }


class ChineseSemanticEngine:
    """Deterministic Traditional-Chinese semantic engine."""

    SCHEMA = SCHEMA
    FEATURES = UNDERSTANDING_FEATURES

    def analyze(self, text: str, *, context: str = "") -> ChineseSemanticAnalysis:
        raw_text = str(text or "")
        normalized_text, synonyms, repairs = self._normalize(raw_text)
        folded = normalized_text.casefold()
        tokens = self._tokenize(normalized_text)
        keyword_counts = self._keywords(tokens)
        intents, matched_terms = self._classify_intents(folded)
        prohibited = self._prohibited_intents(folded)
        actions = self._extract_actions(normalized_text)
        objects = self._extract_objects(normalized_text)
        entities = self._extract_entities(normalized_text)
        negations = self._extract_negations(normalized_text)
        questions = self._extract_questions(normalized_text)
        constraints = self._extract_constraints(normalized_text)
        requested_outputs = self._extract_requested_outputs(normalized_text)
        ellipsis = self._context_completion(
            normalized_text, context, actions, objects
        )
        if ellipsis.get("resolved") is True:
            for item in ellipsis.get("context_objects") or ():
                if item.get("object_type") not in {
                    target.get("object_type") for target in objects
                }:
                    objects.append(dict(item))
            if not matched_terms and str(context or "").strip():
                context_intents, context_terms = self._classify_intents(
                    str(context).casefold()
                )
                intents = list(
                    dict.fromkeys(
                        [
                            *context_intents,
                            *[intent for intent in intents if intent != "conversation"],
                        ]
                    )
                ) or intents
                matched_terms = context_terms
        mixed_language = self._mixed_language(normalized_text)
        destructive = any(
            str(item.get("action")) in DESTRUCTIVE_ACTIONS for item in actions
        )
        ambiguity = self._destructive_ambiguity(normalized_text, objects, entities)
        confirmation_required = bool(destructive and ambiguity)
        safety = {
            "decision": (
                "confirmation-required"
                if confirmation_required
                else "deny"
                if prohibited
                else "continue-under-governance"
            ),
            "operation_allowed": not prohibited,
            "understanding_failed": False,
            "confirmation_required": confirmation_required,
            "confirmation_provided": False,
            "destructive_operation": destructive,
            "remaining_ambiguities": ambiguity,
            "prohibited_surfaces": list(prohibited),
        }
        complexity = {
            "sentence_count": self._sentence_count(raw_text),
            "multi_sentence": self._sentence_count(raw_text) > 1,
            "question_count": len(questions),
            "constraint_count": len(constraints),
        }
        primary_intent = intents[0] if intents else "conversation"
        analysis = ChineseSemanticAnalysis(
            schema=self.SCHEMA,
            raw_text=raw_text,
            normalized_text=normalized_text,
            language=mixed_language["language"],
            language_priority=tuple(mixed_language["language_priority"]),
            token_count=len(tokens),
            tokens=tuple(tokens),
            keywords=tuple(term for term, _ in keyword_counts),
            keyword_counts=tuple(keyword_counts),
            synonyms=tuple(synonyms),
            repairs=tuple(repairs),
            intents=tuple(intents),
            primary_intent=primary_intent,
            matched_terms=matched_terms,
            prohibited_intents=tuple(prohibited),
            actions=tuple(actions),
            operation_objects=tuple(objects),
            entities=entities,
            negations=tuple(negations),
            questions=tuple(questions),
            constraints=tuple(constraints),
            requested_outputs=tuple(requested_outputs),
            ellipsis=ellipsis,
            mixed_language=mixed_language,
            destructive=destructive,
            confirmation_required=confirmation_required,
            safety=safety,
            complexity=complexity,
        )
        from dataclasses import replace

        return replace(analysis, digest=_digest_text(normalized_text))

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize(self, text: str) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
        normalized = unicodedata.normalize("NFKC", text)
        normalized = _fullwidth_to_halfwidth(normalized)
        normalized = re.sub(r"[ \t\u00a0]+", " ", normalized).strip()
        synonyms: list[tuple[str, str]] = []
        repairs: list[tuple[str, str]] = []
        for source, target in TAIWAN_SYNONYMS.items():
            if source in normalized:
                normalized = normalized.replace(source, target)
                synonyms.append((source, target))
        for source, target in COLLOQUIAL_REPAIRS.items():
            if source and source in normalized:
                normalized = normalized.replace(source, target)
                repairs.append((source, target))
        return normalized, synonyms, repairs

    # ------------------------------------------------------------------
    # Tokens and keywords
    # ------------------------------------------------------------------

    def _tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9_.:/@-]*|\d+(?:\.\d+)*|[\u4e00-\u9fff]+", text):
            chunk = match.group(0)
            if re.match(r"[\u4e00-\u9fff]", chunk):
                if len(chunk) == 1:
                    tokens.append(chunk)
                else:
                    tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
            else:
                tokens.append(chunk)
        return tokens

    def _keywords(self, tokens: list[str]) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for token in tokens:
            folded = token.casefold()
            if len(folded) < 2 or folded in STOPWORDS:
                continue
            if re.fullmatch(r"\d+(?:\.\d+)*", folded):
                continue
            counts[folded] = counts.get(folded, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ordered[:20]

    # ------------------------------------------------------------------
    # Intents
    # ------------------------------------------------------------------

    def _classify_intents(
        self, folded: str
    ) -> tuple[list[str], dict[str, tuple[str, ...]]]:
        intents: list[str] = []
        matched: dict[str, tuple[str, ...]] = {}
        for intent, terms in INTENT_LEXICON:
            hits = tuple(term for term in terms if term in folded)
            if hits:
                intents.append(intent)
                matched[intent] = hits
        if not intents:
            intents = ["conversation"]
        return intents, matched

    def _prohibited_intents(self, folded: str) -> list[str]:
        return [
            surface
            for surface in PROHIBITED_SURFACES
            if surface.casefold() in folded
            and any(term in folded for term in ("修改", "刪除", "寫入", "變更", "更新", "改"))
        ]

    # ------------------------------------------------------------------
    # Actions and objects
    # ------------------------------------------------------------------

    def _extract_actions(self, text: str) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for term, action in ACTION_LEXICON:
            start = text.find(term)
            while start != -1:
                actions.append(
                    {
                        "action": action,
                        "matched_text": term,
                        "character_start": start,
                    }
                )
                start = text.find(term, start + len(term))
        actions.sort(key=lambda item: int(item["character_start"]))
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in actions:
            if item["action"] in seen:
                continue
            seen.add(item["action"])
            deduped.append(item)
        return deduped

    def _extract_objects(self, text: str) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for term, object_type in OBJECT_LEXICON:
            start = text.find(term)
            while start != -1:
                objects.append(
                    {
                        "object_type": object_type,
                        "matched_text": term,
                        "character_start": start,
                    }
                )
                start = text.find(term, start + len(term))
        objects.sort(key=lambda item: int(item["character_start"]))
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in objects:
            if item["object_type"] in seen:
                continue
            seen.add(item["object_type"])
            deduped.append(item)
        return deduped

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> dict[str, tuple[str, ...]]:
        def unique(values: list[str]) -> tuple[str, ...]:
            return tuple(dict.fromkeys(value for value in values if value))

        urls = unique(re.findall(r"https?://[^\s\u4e00-\u9fff]+", text))
        emails = unique(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text))
        dates = unique(
            re.findall(
                r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?|民國\d{2,3}年(?:\d{1,2}月)?|"
                r"\d{1,2}月\d{1,2}日|今天|明天|昨天|下週|下個月|今年|明年",
                text,
            )
        )
        percentages = unique(re.findall(r"\d+(?:\.\d+)?\s*%|百分之[\d一二三四五六七八九十百]+", text))
        money = unique(
            re.findall(
                r"(?:NT|US|JP|HK)?\$\s?\d+(?:,\d+)*(?:\.\d+)?|"
                r"\d+(?:,\d+)*(?:\.\d+)?\s?(?:元|萬元|億元|塊)",
                text,
            )
        )
        numbers = unique(re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", text))
        versions = unique(re.findall(r"\bv?\d+\.\d+(?:\.\d+)*\b", text))
        filenames = unique(
            re.findall(r"[\w\u4e00-\u9fff-]+\.(?:py|ts|tsx|js|json|md|sql|txt|csv|xlsx?|pdf|exe|zip|log|ya?ml)", text)
        )
        paths = unique(re.findall(r"[A-Za-z]:\\[^\s\u4e00-\u9fff\"']+", text))
        model_names = unique(
            re.findall(r"\b[a-z][a-z0-9._-]*:[a-z0-9._-]+\b", text)
            + re.findall(r"\b(?:qwen|gemma|llama|deepseek|mistral|granite|nemotron|rnj|glm|gpt-oss)[\w.:-]*", text, flags=re.IGNORECASE)
        )
        quantization = unique(re.findall(r"\b(?:q\d(?:_[a-z0-9]+)*|qat|fp16|fp8|int8|bf16)\b", text, flags=re.IGNORECASE))
        markets = unique(
            [market for label, market in MARKET_ALIASES.items() if label in text]
        )
        symbols = unique(
            [
                match.upper()
                for match in re.findall(
                    r"(?<![A-Za-z0-9])(?:[A-Z]{1,6}|\d{4,6})(?:\.(?:TW|TWO|HK|TO|L))?(?![A-Za-z0-9])",
                    text,
                    flags=re.IGNORECASE,
                )
            ]
        )
        isins = unique(
            [
                match.replace(" ", "").replace("-", "").upper()
                for match in re.findall(r"\b[A-Z]{2}[A-Z0-9 -]{9,14}\d\b", text, flags=re.IGNORECASE)
            ]
        )
        return {
            "urls": urls,
            "emails": emails,
            "dates": dates,
            "percentages": percentages,
            "money": money,
            "numbers": numbers,
            "versions": versions,
            "filenames": filenames,
            "paths": paths,
            "model_names": model_names,
            "quantization": quantization,
            "markets": markets,
            "symbols": symbols,
            "isins": isins,
        }

    # ------------------------------------------------------------------
    # Negation, questions, constraints, outputs
    # ------------------------------------------------------------------

    def _extract_negations(self, text: str) -> list[dict[str, Any]]:
        negations: list[dict[str, Any]] = []
        for marker in NEGATION_MARKERS:
            start = text.casefold().find(marker.casefold())
            while start != -1:
                scope = text[start + len(marker) : start + len(marker) + 24].strip()
                negations.append(
                    {
                        "marker": marker,
                        "character_start": start,
                        "scope": scope,
                    }
                )
                start = text.casefold().find(marker.casefold(), start + len(marker))
        return negations

    def _extract_questions(self, text: str) -> list[str]:
        questions: list[str] = []
        for marker, question_type in QUESTION_MARKERS:
            if marker in text and question_type not in questions:
                questions.append(question_type)
        return questions

    def _extract_constraints(self, text: str) -> list[str]:
        return [marker for marker in CONSTRAINT_MARKERS if marker in text]

    def _extract_requested_outputs(self, text: str) -> list[str]:
        outputs: list[str] = []
        folded = text.casefold()
        for marker, output in REQUESTED_OUTPUT_MARKERS:
            if marker.casefold() in folded and output not in outputs:
                outputs.append(output)
        return outputs

    # ------------------------------------------------------------------
    # Context completion and mixed language
    # ------------------------------------------------------------------

    def _context_completion(
        self,
        text: str,
        context: str,
        actions: list[dict[str, Any]],
        objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        markers = [marker for marker in REFERENCE_MARKERS if marker in text]
        context_text = str(context or "").strip()
        # A command that already names its object is self-contained; only a
        # reference marker without its own object needs the conversation
        # context to complete it.
        needed = bool(markers) and bool(context_text) and not objects
        context_objects: list[dict[str, Any]] = []
        context_actions: list[dict[str, Any]] = []
        if needed:
            for term, object_type in OBJECT_LEXICON:
                if term in context_text:
                    context_objects.append(
                        {"object_type": object_type, "matched_text": term, "source": "context"}
                    )
                    break
            for term, action in ACTION_LEXICON:
                if term in context_text:
                    context_actions.append(
                        {"action": action, "matched_text": term, "source": "context"}
                    )
                    break
        resolved = bool(context_objects or context_actions) or not markers
        return {
            "needed": needed,
            "source": "conversation-history" if needed else "none",
            "resolved": resolved,
            "attempted": needed,
            "context_actions": context_actions,
            "context_objects": context_objects,
            "generic_command": bool(markers and not objects and not actions),
            "reference_markers": markers,
            "resolved_command": text,
        }

    def _mixed_language(self, text: str) -> dict[str, Any]:
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        digits = len(re.findall(r"\d", text))
        total = cjk + latin + digits
        if total == 0:
            language = "unknown"
        elif cjk and latin:
            language = "mixed-zh-latin"
        elif cjk:
            language = "zh-TW"
        elif latin:
            language = "en"
        else:
            language = "numeric"
        priority = (
            ["zh-TW", "mixed-zh-latin", "en"]
            if cjk
            else ["en", "mixed-zh-latin", "zh-TW"]
        )
        return {
            "language": language,
            "language_priority": priority,
            "cjk_characters": cjk,
            "latin_characters": latin,
            "digit_characters": digits,
            "latin_ratio": round(latin / total, 3) if total else 0.0,
        }

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def _destructive_ambiguity(
        self,
        text: str,
        objects: list[dict[str, Any]],
        entities: Mapping[str, tuple[str, ...]],
    ) -> list[str]:
        ambiguity: list[str] = []
        has_target = bool(objects) or any(
            entities.get(key)
            for key in ("filenames", "paths", "model_names", "symbols")
        )
        if not has_target:
            ambiguity.append("destructive-target-missing")
        if any(token in text for token in ("全部", "所有", "整個")):
            ambiguity.append("bulk-scope")
        if (
            any(token in text for token in ("那個", "這個", "它", "他", "她"))
            and not has_target
        ):
            ambiguity.append("unresolved-reference")
        return ambiguity

    @staticmethod
    def _sentence_count(text: str) -> int:
        return len([part for part in re.split(r"[。！？!?;；\n]+", text) if part.strip()]) or (
            1 if text.strip() else 0
        )


__all__ = [
    "ACTION_LEXICON",
    "COLLOQUIAL_REPAIRS",
    "DESTRUCTIVE_ACTIONS",
    "INTENT_LEXICON",
    "NEGATION_MARKERS",
    "OBJECT_LEXICON",
    "PROHIBITED_SURFACES",
    "QUESTION_MARKERS",
    "SCHEMA",
    "TAIWAN_SYNONYMS",
    "UNDERSTANDING_FEATURES",
    "ChineseSemanticAnalysis",
    "ChineseSemanticEngine",
]
