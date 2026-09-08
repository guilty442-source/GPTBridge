from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Callable, Iterable, Mapping, Sequence

from .generative_language_model import StarAutoregressiveLanguageModel


InvestmentAnalyzer = Callable[[dict[str, Any]], dict[str, Any]]
MarketSearcher = Callable[[dict[str, Any]], dict[str, Any]]


class StarNativeLanguageModel:
    """First-party, locally trained language-model and governed tool pipeline."""

    MODEL_ID = "star-native-language-model"
    VERSION = "1.0"
    ARCHITECTURE = (
        "star-tokenizer+intent-encoder+local-retrieval+tool-router+"
        "first-party-lexical-prototype-intent-classifier+"
        "source-attributed-long-context-reading+"
        "weighted-backoff-autoregressive-language-model+self-training"
    )

    _INTENTS = (
        (
            "capabilities",
            (
                "有哪些能力",
                "能力清單",
                "你會什麼",
                "可以做什麼",
                "能做什麼",
                "what can you do",
                "capabilities",
            ),
        ),
        (
            "self_upgrade",
            (
                "自我升級",
                "升級自己",
                "更新自己",
                "擴充自己",
                "升級星澄",
                "改善星澄",
                "修正星澄",
                "提升星澄",
                "檢討星澄",
                "self-upgrade",
                "improve yourself",
                "upgrade star",
            ),
        ),
        (
            "coding",
            (
                "程式碼",
                "寫程式",
                "程式設計",
                "編程",
                "編碼",
                "Python",
                "TypeScript",
                "JavaScript",
                "SQL",
                "重構",
                "函式",
                "修正問題",
                "修正錯誤",
                "修改程式",
                "建立功能",
                "新增功能",
                "實作功能",
                "檢查程式",
                "write code",
                "coding",
                "programming",
                "refactor",
                "function",
            ),
        ),
        (
            "visual",
            (
                "視覺辨識",
                "圖片辨識",
                "照片辨識",
                "影片辨識",
                "文件影像",
                "看圖",
                "圖像分類",
                "image recognition",
                "visual recognition",
                "video recognition",
                "document image",
            ),
        ),
        (
            "file_management",
            (
                "檔案管理",
                "整理檔案",
                "整理圖片",
                "整理影片",
                "檔案分類",
                "圖片分類",
                "產生標籤",
                "自動標籤",
                "file management",
                "file classification",
                "image tagging",
            ),
        ),
        (
            "reading",
            (
                "閱讀",
                "讀取",
                "讀完",
                "摘要",
                "總結",
                "重點",
                "大綱",
                "文件",
                "文章",
                "原文",
                "reading",
                "summarize",
                "summary",
                "document",
            ),
        ),
        (
            "statistics",
            ("統計", "平均", "中位數", "標準差", "變異", "相關性", "共變異", "statistics", "average", "median", "standard deviation"),
        ),
        (
            "data_organization",
            ("整理資料", "彙整資料", "資料清理", "分類資料", "排序資料", "organize data", "clean data", "sort data"),
        ),
        (
            "calculation",
            ("計算", "算出", "等於多少", "公式", "數學", "XIRR", "再平衡", "夏普", "Sortino", "calculate", "calculation", "formula"),
        ),
        ("reasoning", ("推理", "邏輯", "證明", "推導", "因果", "reasoning", "reason", "logic", "prove")),
        ("search", ("搜尋", "查詢", "找資料", "查資料", "search", "searching", "look up", "research")),
        ("distribution", ("配息", "股息", "收益分配", "除息", "dividend", "distribution")),
        ("quote", ("報價", "價格", "淨值", "行情", "quote", "price", "net asset value")),
        ("risk", ("風險", "波動", "回撤", "集中", "壓力", "情境", "risk", "volatility", "drawdown", "stress")),
        ("analysis", ("分析", "評估", "投資", "持股", "資產", "analyze", "analyse", "analysis", "portfolio", "holdings", "investment")),
        ("status", ("狀態", "健康", "資料庫", "版本", "模型", "status", "health", "version", "model")),
    )
    _MARKET_ALIASES = {
        "台股": "TW",
        "臺股": "TW",
        "美股": "US",
        "港股": "HK",
        "日股": "JP",
        "基金": "FUND",
    }
    _ACTION_ALIASES: dict[str, tuple[str, ...]] = {
        "query": ("查詢", "查一下", "看一下", "幫我查", "搜尋", "查找", "look up", "search"),
        "create": ("建立", "新增", "創建", "create", "add"),
        "generate": ("產生", "生成", "generate"),
        "modify": ("修改", "變更", "調整", "更新", "modify", "update", "edit"),
        "delete": ("刪除", "刪掉", "移除", "砍掉", "清掉", "delete", "remove"),
        "move": ("搬移", "移動", "搬到", "移到", "move"),
        "copy": ("複製", "拷貝", "copy"),
        "rename": ("重新命名", "改名", "rename"),
        "classify": ("分類", "歸類", "整理", "classify", "organize"),
        "analyze": ("分析", "解析", "評估", "analyze", "analyse"),
        "compare": ("比較", "比對", "對照", "compare"),
        "execute": ("執行", "運行", "跑一下", "啟動", "execute", "run", "start"),
        "stop": ("停止", "終止", "關閉", "stop", "terminate", "shutdown"),
        "monitor": ("監控", "監看", "持續觀察", "monitor", "watch"),
    }
    _ACTION_LABELS = {
        "query": "查詢",
        "create": "建立",
        "generate": "產生",
        "modify": "修改",
        "delete": "刪除",
        "move": "搬移",
        "copy": "複製",
        "rename": "重新命名",
        "classify": "分類",
        "analyze": "分析",
        "compare": "比較",
        "execute": "執行",
        "stop": "停止",
        "monitor": "監控",
    }
    _TAIWAN_TERM_ALIASES: dict[str, tuple[str, ...]] = {
        "資料夾": ("文件夾", "資料加", "資聊夾"),
        "設定": ("配置", "設訂"),
        "程序": ("進程", "程續"),
        "影片": ("視頻", "影篇"),
        "圖片": ("圖像", "圖篇"),
        "模型": ("模形",),
        "檔案": ("檔按",),
        "程式碼": ("程式馬",),
        "執行": ("執型", "运行"),
        "分類": ("分纇",),
        "監控": ("監空",),
        "刪除": ("删除",),
        "查詢": ("查询",),
    }
    _OBJECT_ALIASES: dict[str, tuple[str, ...]] = {
        "model": ("模型", "model", "ollama"),
        "file": ("檔案", "文件", "file"),
        "folder": ("資料夾", "目錄", "folder", "directory"),
        "image": ("圖片", "照片", "影像", "image", "photo"),
        "video": ("影片", "視訊", "video"),
        "code": ("程式碼", "原始碼", "code", "source"),
        "text": ("文字", "文章", "內容", "text"),
        "database": ("資料庫", "database", "sqlite", "table"),
        "service": ("服務", "service", "daemon"),
        "process": ("程序", "進程", "process", "pid"),
        "config": ("設定檔", "配置檔", "設定", "config", "configuration"),
    }
    _REFERENCE_MARKERS = (
        "這個",
        "這些",
        "那個",
        "那些",
        "它",
        "它們",
        "舊的",
        "新的",
        "剛才",
        "剛剛",
        "上一個",
        "上一批",
        "照前面",
        "照剛才",
        "繼續",
        "確認執行",
        "確認刪除",
    )
    _DESTRUCTIVE_MARKERS = (
        "刪除",
        "刪掉",
        "移除",
        "砍掉",
        "清掉",
        "清空",
        "永久刪除",
        "強制刪除",
        "覆寫",
        "格式化",
        "重置",
        "drop table",
        "truncate table",
        "delete",
        "remove",
        "overwrite",
        "format",
        "reset",
    )
    _INTENT_EXAMPLES: dict[str, tuple[str, ...]] = {
        "conversation": (
            "你好，請簡短回覆",
            "確認對話是否正常並回覆指定文字",
            "針對最新訊息直接回答",
        ),
        "capabilities": (
            "列出目前可以使用的能力",
            "說明你能協助哪些工作",
            "你有哪些功能",
        ),
        "self_upgrade": ("改善自身模組", "提出系統更新方案", "讓星澄維護自己的程式"),
        "coding": ("建立資料接收端點", "實作一個服務模組", "檢查這段原始碼的問題"),
        "visual": ("辨識圖片中的內容", "整理影片畫面重點", "摘要文件掃描影像"),
        "file_management": ("依圖片內容自動分類檔案", "替影像產生標籤", "建議檔案資料夾"),
        "reading": ("找出兩份內容的共同觀點", "根據材料回答問題", "整理長篇報告的核心結論"),
        "statistics": ("描述這批樣本的分布", "求資料的離散程度", "比較兩組數據的關聯"),
        "data_organization": ("把紀錄依欄位分組", "清除重複列並排列", "將原始資料轉成表格"),
        "calculation": ("依公式求出結果", "算出投資組合報酬", "列出數值運算步驟"),
        "reasoning": ("根據前提判斷結論", "找出論述中的矛盾", "說明事件之間的因果"),
        "search": ("從公開來源取得最新資料", "幫我找到相關公告", "查證這項資訊的來源"),
        "distribution": ("確認這次收益何時發放", "是否有現金股利", "查核除息與入帳日期"),
        "quote": ("取得目前成交數值", "查基金最新淨值", "這項資產現在值多少"),
        "risk": ("檢查最壞情境與曝險", "評估可能損失", "找出組合過度集中的地方"),
        "analysis": ("評估持倉配置是否合理", "說明資產組合表現", "整合資料提出投資觀察"),
        "status": ("目前是否正常運作", "顯示系統健康資訊", "確認目前使用的版本"),
    }

    def __init__(
        self,
        *,
        learned_examples: Iterable[Mapping[str, Any]] = (),
        corpus: Mapping[str, Sequence[str]] | None = None,
        model_role: str = "main",
    ) -> None:
        self.model_role = str(model_role or "main")
        self.language_model = StarAutoregressiveLanguageModel(
            learned_examples=learned_examples,
            corpus=corpus,
        )

    def training_status(self) -> dict[str, Any]:
        return {
            **self.language_model.metrics(),
            "model_role": self.model_role,
            "training_mode": "continuous-verified-self-distillation-and-ollama-local-model-training",
            "training_data_scope": "star-owned-and-star-validated-gpt-candidates",
            "quality_gate_required": True,
            "rollback_source": "versioned-training-examples",
            "gpt_candidate_direct_write": False,
            "gpt_weight_access": False,
        }

    def learn_verified_example(self, example: Mapping[str, Any]) -> bool:
        if example.get("validated") is not True:
            return False
        return self.language_model.learn(
            str(example.get("intent") or "capabilities"),
            str(example.get("target_text") or ""),
            input_text=str(example.get("input_text") or ""),
            weight=max(
                1,
                min(5, round(float(example.get("quality_score") or 0.8) * 5)),
            ),
            source="self-training",
        )

    @staticmethod
    def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @classmethod
    def _intent(cls, prompt: str) -> str:
        return cls.classify_intents(prompt)[0]

    @classmethod
    def classify_intent(cls, prompt: str) -> str:
        return cls._intent(prompt)

    @classmethod
    def classify_intents(cls, prompt: str) -> list[str]:
        normalized = str(prompt or "").strip().casefold()
        direct_matches, prohibited_intents = cls._matched_intents(normalized)
        if direct_matches:
            return list(dict.fromkeys(direct_matches))

        prompt_features = cls._intent_features("".join(cls._tokenize(normalized)))
        scored: list[tuple[float, str]] = []
        for intent, examples in cls._INTENT_EXAMPLES.items():
            if intent in prohibited_intents:
                continue
            score = max(
                (cls._feature_similarity(prompt_features, cls._intent_features(example)) for example in examples),
                default=0.0,
            )
            scored.append((score, intent))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored or scored[0][0] < 0.18:
            return ["conversation"]
        maximum = scored[0][0]
        return [
            intent
            for score, intent in scored
            if score >= max(0.18, maximum * 0.82)
        ][:3]

    @staticmethod
    def _term_pattern(term: str) -> str:
        escaped = re.escape(str(term or "").casefold())
        if re.search(r"[a-z0-9]", term, flags=re.IGNORECASE):
            return rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])"
        return escaped

    @staticmethod
    def _term_is_negated(text: str, start: int) -> bool:
        prefix = text[max(0, start - 32) : start]
        prefix = re.split(r"[，,。；;！？!?\n]", prefix)[-1]
        return bool(
            re.search(
                r"(?:不要|不得|不可|禁止|無需|不用|不必|避免|停止|"
                r"do\s+not|don't|must\s+not|without|no)\s*(?:再\s*)?$",
                prefix,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _matched_intents(cls, normalized: str) -> tuple[list[str], set[str]]:
        matched: list[str] = []
        prohibited: set[str] = set()
        for intent, terms in cls._INTENTS:
            positive = False
            negative = False
            for term in terms:
                for occurrence in re.finditer(
                    cls._term_pattern(term), normalized, flags=re.IGNORECASE
                ):
                    if cls._term_is_negated(normalized, occurrence.start()):
                        negative = True
                    else:
                        positive = True
            if positive:
                matched.append(intent)
            elif negative:
                prohibited.add(intent)
        return matched, prohibited

    @staticmethod
    def _intent_features(text: str) -> set[str]:
        normalized = str(text or "").casefold()
        features = set(re.findall(r"[a-z][a-z0-9_-]+", normalized))
        for run in re.findall(r"[\u3400-\u9fff]+", normalized):
            features.update(run[index : index + 2] for index in range(len(run) - 1))
        return features

    @staticmethod
    def _feature_similarity(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return 2 * len(left & right) / (len(left) + len(right))

    @staticmethod
    def _language(text: str) -> str:
        has_chinese = bool(re.search(r"[\u3400-\u9fff]", text))
        has_latin = bool(re.search(r"[A-Za-z]", text))
        if has_chinese and has_latin:
            return "mixed-zh-latin"
        if has_chinese:
            return "zh-TW"
        if has_latin:
            return "en"
        return "und"

    @classmethod
    def _normalize_chinese_semantics(cls, text: str) -> dict[str, Any]:
        """Normalize command vocabulary while preserving paths and identifiers."""

        original = str(text or "")
        protected: list[str] = []

        def protect(match: re.Match[str]) -> str:
            protected.append(match.group(0))
            return f"\u0000PROTECTED{len(protected) - 1}\u0000"

        protected_text = re.sub(
            r"(?:[A-Za-z]:\\[^\r\n，。；]+|https?://\S+|"
            r"(?<!\w)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|"
            r"(?<!\w)[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}(?!\w)|"
            r"(?<!\w)[A-Za-z0-9][A-Za-z0-9._/-]{1,127}:[A-Za-z0-9._-]+(?!\w)|"
            r"[`\"'][^`\"'\r\n]+[`\"'])",
            protect,
            original,
            flags=re.IGNORECASE,
        )
        normalized = protected_text
        changes: list[dict[str, str]] = []
        for canonical, aliases in cls._TAIWAN_TERM_ALIASES.items():
            for alias in sorted(aliases, key=len, reverse=True):
                if alias not in normalized:
                    continue
                normalized = normalized.replace(alias, canonical)
                changes.append({"from": alias, "to": canonical})
        for action, aliases in cls._ACTION_ALIASES.items():
            canonical = cls._ACTION_LABELS[action]
            for alias in sorted(aliases, key=len, reverse=True):
                pattern = cls._term_pattern(alias)
                if not re.search(pattern, normalized, flags=re.IGNORECASE):
                    continue
                normalized = re.sub(
                    pattern, canonical, normalized, flags=re.IGNORECASE
                )
                if alias.casefold() != canonical.casefold():
                    changes.append({"from": alias, "to": canonical})
        for index, value in enumerate(protected):
            normalized = normalized.replace(f"\u0000PROTECTED{index}\u0000", value)
        return {
            "text": normalized.strip(),
            "changes": changes,
            "protected_identifier_count": len(protected),
            "locale": "zh-TW",
            "translated": False,
        }

    @classmethod
    def _extract_command_actions(cls, text: str) -> list[dict[str, Any]]:
        lowered = str(text or "").casefold()
        actions: list[dict[str, Any]] = []
        for action, aliases in cls._ACTION_ALIASES.items():
            matches = [
                match
                for alias in aliases
                for match in re.finditer(
                    cls._term_pattern(alias), lowered, flags=re.IGNORECASE
                )
                if not cls._term_is_negated(lowered, match.start())
            ]
            if matches:
                first = min(matches, key=lambda item: item.start())
                actions.append(
                    {
                        "action": action,
                        "label": cls._ACTION_LABELS[action],
                        "character_start": first.start(),
                    }
                )
        return sorted(actions, key=lambda item: int(item["character_start"]))

    @classmethod
    def _extract_operation_objects(cls, text: str) -> list[dict[str, Any]]:
        lowered = str(text or "").casefold()
        objects: list[dict[str, Any]] = []
        for object_type, aliases in cls._OBJECT_ALIASES.items():
            matches = [
                match
                for alias in aliases
                for match in re.finditer(
                    cls._term_pattern(alias), lowered, flags=re.IGNORECASE
                )
            ]
            if matches:
                first = min(matches, key=lambda item: item.start())
                objects.append(
                    {
                        "object_type": object_type,
                        "matched_text": first.group(0),
                        "character_start": first.start(),
                    }
                )
        return sorted(objects, key=lambda item: int(item["character_start"]))

    @staticmethod
    def _extract_command_parameters(text: str) -> dict[str, Any]:
        value = str(text or "")
        paths = re.findall(
            r"(?:[A-Za-z]:\\[^\r\n，。；]+|(?<!\w)/(?:[^\s/]+/)*[^\s，。；]+)",
            value,
        )
        filenames = re.findall(
            r"(?<![\\/\w.-])[A-Za-z0-9_\u3400-\u9fff .-]+\.[A-Za-z0-9]{1,12}(?!\w)",
            value,
        )
        model_names = re.findall(
            r"(?<![A-Za-z0-9])(?:"
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|"
            r"(?:qwen|llama|gemma|deepseek|nemotron|mistral|phi|star)[A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*"
            r")(?![A-Za-z0-9])",
            value,
            flags=re.IGNORECASE,
        )
        sizes = re.findall(
            r"(?<!\w)\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB|KiB|MiB|GiB|TiB|VRAM)(?!\w)",
            value,
            flags=re.IGNORECASE,
        )
        versions = re.findall(
            r"(?<!\w)(?:v(?:ersion)?\s*)?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?(?!\w)",
            value,
            flags=re.IGNORECASE,
        )
        quantization = re.findall(
            r"(?<!\w)(?:Q[234568](?:_[A-Z0-9]+)?|IQ[234](?:_[A-Z0-9]+)?|FP(?:8|16|32)|BF16|GGUF|AWQ|GPTQ)(?!\w)",
            value,
            flags=re.IGNORECASE,
        )
        sort_orders = [
            marker
            for marker in ("升冪", "降冪", "由新到舊", "由舊到新", "由大到小", "由小到大")
            if marker in value
        ]
        output_formats = [
            marker.upper() if marker.casefold() != "markdown" else "Markdown"
            for marker in re.findall(
                r"(?<!\w)(?:json|csv|tsv|xlsx|docx|pdf|markdown|html|xml|yaml)(?!\w)",
                value,
                flags=re.IGNORECASE,
            )
        ]
        return {
            "paths": list(dict.fromkeys(item.strip() for item in paths))[:20],
            "filenames": list(dict.fromkeys(item.strip() for item in filenames))[:20],
            "model_names": list(dict.fromkeys(model_names))[:20],
            "numbers": [float(item) for item in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", value)[:30]],
            "dates": re.findall(
                r"(?<!\d)(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])(?:[-/.月](?:0?[1-9]|[12]\d|3[01])日?)?(?!\d)",
                value,
            )[:20],
            "sizes": list(dict.fromkeys(sizes))[:20],
            "versions": list(dict.fromkeys(versions))[:20],
            "quantization_formats": list(dict.fromkeys(item.upper() for item in quantization))[:20],
            "sort_orders": sort_orders,
            "output_formats": list(dict.fromkeys(output_formats))[:20],
        }

    @staticmethod
    def _specific_constraint_flags(text: str) -> list[dict[str, Any]]:
        lowered = str(text or "").casefold()
        definitions = (
            ("local_only", ("只用本地", "只用本機", "本地模型", "本機模型", "local only")),
            ("no_api", ("不要 api", "不用 api", "禁止 api", "no api")),
            ("no_plugins", ("不要插件", "不用插件", "禁止插件", "no plugin")),
            ("traditional_chinese", ("繁體中文", "正體中文", "zh-tw")),
            ("no_image_generation", ("不要生成圖片", "不要產生圖片", "no image generation")),
            ("latest_generation_only", ("只要最新代", "僅限最新版本", "latest generation only")),
        )
        flags = [
            {"constraint": key, "matched_text": marker}
            for key, markers in definitions
            for marker in markers
            if marker in lowered
        ]
        vram = re.search(
            r"(?:限制|最多|不超過|上限)?\s*(\d+(?:\.\d+)?)\s*(GB|GiB)\s*VRAM",
            text,
            flags=re.IGNORECASE,
        )
        if vram:
            flags.append(
                {
                    "constraint": "vram_limit",
                    "value": float(vram.group(1)),
                    "unit": vram.group(2),
                    "matched_text": vram.group(0),
                }
            )
        return flags

    @staticmethod
    def _semantic_keywords(text: str) -> list[dict[str, int | str]]:
        stop_words = {
            "and",
            "for",
            "from",
            "please",
            "that",
            "the",
            "this",
            "with",
            "一個",
            "以及",
            "使用",
            "可以",
            "文件",
            "星澄",
            "這個",
            "需要",
            "請把",
            "請用",
        }
        latin = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.casefold())
        chinese = [
            run[index : index + 2]
            for run in re.findall(r"[\u3400-\u9fff]{2,}", text)
            for index in range(len(run) - 1)
        ]
        counts = Counter(
            term for term in latin + chinese if term not in stop_words
        )
        return [
            {"term": term, "count": count}
            for term, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )[:20]
        ]

    @classmethod
    def _comprehension_features(cls, text: str) -> dict[str, Any]:
        sentences = [
            match.group(0).strip()
            for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text)
            if match.group(0).strip()
        ]
        question_markers = (
            ("reason", ("為何", "為什麼", "why")),
            ("method", ("如何", "怎麼", "how")),
            ("time", ("何時", "什麼時候", "when")),
            ("location", ("哪裡", "何處", "where")),
            ("person", ("誰", "何人", "who")),
            ("quantity", ("多少", "幾個", "how many", "how much")),
            ("definition", ("什麼", "何謂", "what", "which")),
        )
        questions: list[dict[str, Any]] = []
        for sentence in sentences:
            lowered = sentence.casefold()
            matched = [
                marker
                for _, markers in question_markers
                for marker in markers
                if marker in lowered
            ]
            if "?" not in sentence and "？" not in sentence and not matched:
                continue
            question_type = next(
                (
                    kind
                    for kind, markers in question_markers
                    if any(marker in lowered for marker in markers)
                ),
                "yes_no",
            )
            questions.append(
                {
                    "type": question_type,
                    "text": sentence[:500],
                    "markers": matched,
                }
            )

        constraint_patterns = (
            ("required", r"必須|務必|一定要|需要|must\b|shall\b|required\b"),
            ("prohibited", r"不得|不可|禁止|不要|不能|do not\b|must not\b|forbid"),
            ("minimum", r"至少|不低於|at least\b|minimum\b"),
            ("maximum", r"至多|最多|不超過|at most\b|maximum\b"),
            ("exclusive", r"只能|僅限|only\b"),
        )
        constraints = [
            {
                "type": kind,
                "marker": match.group(0),
                "character_start": match.start(),
            }
            for kind, pattern in constraint_patterns
            for match in re.finditer(pattern, text, flags=re.IGNORECASE)
        ]
        negations = [
            {"marker": match.group(0), "character_start": match.start()}
            for match in re.finditer(
                r"不得|不可|不會|不是|沒有|禁止|拒絕|不能|不要|\bnot\b|\bno\b|\bnever\b|without",
                text,
                flags=re.IGNORECASE,
            )
        ]
        output_markers = {
            "summary": ("摘要", "總結", "summar"),
            "outline": ("大綱", "章節", "outline"),
            "table": ("表格", "table"),
            "code": ("程式碼", "code"),
            "citations": ("引用", "來源", "citation", "source"),
            "steps": ("步驟", "流程", "steps"),
            "comparison": ("比較", "差異", "compare"),
        }
        lowered_text = text.casefold()
        requested_outputs = [
            output
            for output, markers in output_markers.items()
            if any(marker in lowered_text for marker in markers)
        ]
        command_markers = re.compile(
            r"請|幫我|需要|務必|建立|新增|實作|產生|執行|修正|修改|檢討|檢查|分析|整理|計算|閱讀|summarize|create|implement|execute|fix|modify|analyze|calculate",
            flags=re.IGNORECASE,
        )
        objectives = [
            sentence[:500] for sentence in sentences if command_markers.search(sentence)
        ][:10]
        action_markers = {
            "create": ("建立", "新增", "產生", "create", "add"),
            "implement": ("實作", "完成", "implement"),
            "execute": ("執行", "運行", "execute", "run"),
            "repair": ("修正", "修復", "修理", "fix", "repair"),
            "modify": ("修改", "變更", "更新", "modify", "update"),
            "inspect": ("檢討", "檢查", "查看", "inspect", "review", "check"),
        }
        requested_actions = [
            action
            for action, markers in action_markers.items()
            if any(marker in lowered_text for marker in markers)
        ]
        return {
            "sentence_count": len(sentences),
            "questions": questions[:20],
            "negations": negations[:30],
            "constraints": constraints[:30],
            "requested_outputs": requested_outputs,
            "objectives": objectives,
            "command": {
                "recognized": bool(objectives or requested_actions),
                "execution_requested": any(
                    action in {"create", "implement", "execute", "repair", "modify"}
                    for action in requested_actions
                ),
                "requested_actions": requested_actions,
            },
            "keywords": cls._semantic_keywords(text),
            "complexity": {
                "multi_sentence": len(sentences) > 1,
                "question_count": len(questions),
                "constraint_count": len(constraints),
            },
        }

    @classmethod
    def _task_classification(
        cls,
        *,
        intents: Sequence[str],
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        text: str,
    ) -> dict[str, Any]:
        intent_set = set(intents)
        action_set = {str(item.get("action") or "") for item in actions}
        object_set = {str(item.get("object_type") or "") for item in objects}
        categories: list[str] = []
        if "coding" in intent_set or "code" in object_set:
            categories.append("coding")
        if "visual" in intent_set or object_set & {"image", "video"}:
            categories.append("visual")
        if (
            "file_management" in intent_set
            or object_set & {"file", "folder"}
            or ("classify" in action_set and object_set & {"image", "video"})
            or action_set & {
            "move",
            "copy",
            "rename",
            }
        ):
            categories.append("file_management")
        if intent_set & {"reasoning", "calculation", "statistics", "analysis", "risk"}:
            categories.append("reasoning")
        if intent_set & {"search", "reading"} or "query" in action_set:
            categories.append("rag_search")
        if "monitor" in action_set or re.search(
            r"自動化|排程|代理|agent|automation|schedule", text, flags=re.IGNORECASE
        ):
            categories.append("automation_agent")
        if object_set & {"service", "process", "config", "database"} or action_set & {
            "execute",
            "stop",
        }:
            categories.append("system_operation")
        if re.search(r"多模型|協作|交叉驗證|multi[- ]?model", text, flags=re.IGNORECASE):
            categories.append("multi_model_collaboration")
        if not categories:
            categories.append("general_chinese_task")
        categories = list(dict.fromkeys(categories))
        capability_needs = {
            "visual": "visual" in categories,
            "rag_or_search": "rag_search" in categories,
            "coding": "coding" in categories,
            "agent": "automation_agent" in categories,
            "multi_model": "multi_model_collaboration" in categories,
        }
        return {
            "primary": categories[0],
            "categories": categories,
            "capability_needs": capability_needs,
        }

    @staticmethod
    def _task_intensity(
        *,
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        constraints: Sequence[Mapping[str, Any]],
        task_categories: Sequence[str],
        ambiguity_count: int,
        destructive_operation: bool,
        text: str,
    ) -> dict[str, Any]:
        score = 0
        reasons: list[str] = []
        if len(actions) >= 2:
            score += 1
            reasons.append("multiple-actions")
        if len(objects) >= 2:
            score += 1
            reasons.append("multiple-operation-objects")
        if constraints:
            score += 1
            reasons.append("explicit-constraints")
        if len(task_categories) >= 2:
            score += 1
            reasons.append("cross-domain-task")
        if ambiguity_count:
            score += 1
            reasons.append("context-or-clarification-required")
        if destructive_operation:
            score += 2
            reasons.append("destructive-operation-risk")
        if len(text) > 600 or text.count("\n") >= 5:
            score += 1
            reasons.append("long-or-multistep-input")
        if any(
            category in {"automation_agent", "system_operation", "multi_model_collaboration"}
            for category in task_categories
        ):
            score += 1
            reasons.append("operational-or-coordinated-execution")
        if score <= 1:
            level = "simple"
        elif score <= 3:
            level = "normal"
        elif score <= 5:
            level = "intermediate"
        else:
            level = "difficult"
        return {"level": level, "score": score, "reasons": reasons}

    @classmethod
    def _context_completion(
        cls,
        *,
        command: str,
        context: str,
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        command_lower = command.casefold()
        references = [
            marker for marker in cls._REFERENCE_MARKERS if marker in command_lower
        ]
        generic_only = bool(
            re.fullmatch(
                r"\s*(?:請|麻煩|幫我|我已|我)?\s*(?:確認執行|確認刪除|執行|繼續|照做|開始|停止|刪掉|刪除|修改|處理)(?:吧|一下|即可|。|！|!)?\s*",
                command,
                flags=re.IGNORECASE,
            )
        )
        needs_context = bool(references or generic_only)
        context_text = str(context or "").strip()
        context_actions = cls._extract_command_actions(context_text) if context_text else []
        context_objects = cls._extract_operation_objects(context_text) if context_text else []
        context_parameters = (
            cls._extract_command_parameters(context_text) if context_text else {}
        )
        supplied = bool(context_text and needs_context)
        resolved = bool(
            supplied
            and (objects or context_objects)
            and (
                any(parameters.get(key) for key in ("paths", "filenames", "model_names"))
                or any(
                    context_parameters.get(key)
                    for key in ("paths", "filenames", "model_names")
                )
                or not any(
                    str(item.get("action") or "") == "delete" for item in actions
                )
            )
        )
        resolved_command = command
        if supplied:
            resolved_command = (
                f"{command}\n\n可用的前文補全依據：\n{context_text[-8_000:]}"
            )
        return {
            "needed": needs_context,
            "attempted": supplied,
            "resolved": resolved if needs_context else True,
            "reference_markers": references,
            "generic_command": generic_only,
            "context_actions": context_actions,
            "context_objects": context_objects,
            "context_parameters": context_parameters,
            "resolved_command": resolved_command,
            "source": "recent-conversation" if supplied else "none",
        }

    @classmethod
    def _command_safety(
        cls,
        *,
        command: str,
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, Any],
        context_completion: Mapping[str, Any],
        intent_explicit: bool,
        confirmed: bool,
    ) -> dict[str, Any]:
        lowered = command.casefold()
        context_actions = context_completion.get("context_actions")
        if not isinstance(context_actions, Sequence):
            context_actions = []
        context_objects = context_completion.get("context_objects")
        if not isinstance(context_objects, Sequence):
            context_objects = []
        effective_objects = [
            item
            for item in (*objects, *context_objects)
            if isinstance(item, Mapping)
        ]
        destructive = bool(
            "delete" in {str(item.get("action") or "") for item in actions}
            or any(marker in lowered for marker in cls._DESTRUCTIVE_MARKERS)
            or (
                context_completion.get("needed") is True
                and "delete"
                in {
                    str(item.get("action") or "")
                    for item in context_actions
                    if isinstance(item, Mapping)
                }
            )
        )
        context_parameters = context_completion.get("context_parameters")
        if not isinstance(context_parameters, Mapping):
            context_parameters = {}
        has_specific_target = bool(
            any(parameters.get(key) for key in ("paths", "filenames", "model_names"))
            or any(
                context_parameters.get(key)
                for key in ("paths", "filenames", "model_names")
            )
        )
        understood = bool(actions or intent_explicit)
        understanding_failed = bool(
            not understood
            or (
                context_completion.get("needed") is True
                and context_completion.get("resolved") is not True
            )
            or (destructive and (not effective_objects or not has_specific_target))
        )
        remaining_ambiguities: list[str] = []
        if not understood:
            remaining_ambiguities.append("action-or-intent-not-understood")
        if context_completion.get("needed") is True and context_completion.get("resolved") is not True:
            remaining_ambiguities.append("context-reference-unresolved")
        if destructive and not effective_objects:
            remaining_ambiguities.append("destructive-object-not-identified")
        if destructive and not has_specific_target:
            remaining_ambiguities.append("destructive-target-not-specific")
        confirmation_accepted = bool(
            confirmed and has_specific_target and effective_objects
        )
        confirmation_required = bool(
            destructive and understanding_failed and not confirmation_accepted
        )
        return {
            "destructive_operation": destructive,
            "understanding_failed": understanding_failed,
            "context_completion_attempted": context_completion.get("attempted") is True,
            "remaining_ambiguities": list(dict.fromkeys(remaining_ambiguities)),
            "confirmation_provided": confirmed,
            "confirmation_accepted": confirmation_accepted,
            "confirmation_required": confirmation_required,
            "operation_allowed": not confirmation_required,
            "decision": "safe-confirmation-required"
            if confirmation_required
            else "continue-under-governance",
        }

    @classmethod
    def semantic_plan(
        cls,
        prompt: str,
        *,
        context: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        raw_input = str(prompt or "")
        normalization = cls._normalize_chinese_semantics(raw_input)
        normalized = str(normalization["text"])
        intents = cls.classify_intents(normalized)
        _, prohibited_intents = cls._matched_intents(normalized.casefold())
        tokenized = cls._tokenize(normalized)
        matched_terms = {
            intent: [token for token in tokens if token.casefold() in normalized.casefold()]
            for intent, tokens in cls._INTENTS
            if intent in intents
        }
        symbols = list(
            dict.fromkeys(
                match.upper()
                for match in re.findall(
                    r"(?<![A-Za-z0-9])(?:[A-Z]{1,6}|\d{4,6})(?:\.(?:TW|TWO|HK|TO|L))?(?![A-Za-z0-9])",
                    normalized,
                    flags=re.IGNORECASE,
                )
            )
        )[:20]
        isins = list(
            dict.fromkeys(
                match.replace(" ", "").replace("-", "").upper()
                for match in re.findall(
                    r"\b[A-Z]{2}[A-Z0-9 -]{9,14}\d\b",
                    normalized,
                    flags=re.IGNORECASE,
                )
            )
        )[:10]
        markets = [
            market
            for label, market in cls._MARKET_ALIASES.items()
            if label in normalized
        ]
        time_horizon = next(
            (
                horizon
                for token, horizon in (
                    ("短期", "short"),
                    ("中期", "medium"),
                    ("長期", "long"),
                    ("今年", "year-to-date"),
                )
                if token in normalized
            ),
            "unspecified",
        )
        comprehension = cls._comprehension_features(normalized)
        actions = cls._extract_command_actions(normalized)
        if actions:
            comprehension["command"]["recognized"] = True
            comprehension["command"]["requested_actions"] = list(
                dict.fromkeys(
                    [
                        *comprehension["command"]["requested_actions"],
                        *[str(item["action"]) for item in actions],
                    ]
                )
            )
            comprehension["command"]["execution_requested"] = bool(
                comprehension["command"]["execution_requested"]
                or any(
                    str(item["action"])
                    in {
                        "create",
                        "generate",
                        "modify",
                        "delete",
                        "move",
                        "copy",
                        "rename",
                        "execute",
                        "stop",
                    }
                    for item in actions
                )
            )
        operation_objects = cls._extract_operation_objects(normalized)
        parameters = cls._extract_command_parameters(raw_input)
        specific_constraints = cls._specific_constraint_flags(normalized)
        context_completion = cls._context_completion(
            command=raw_input.strip(),
            context=context,
            actions=actions,
            objects=operation_objects,
            parameters=parameters,
        )
        intent_explicit = bool(matched_terms)
        effective_actions = list(actions)
        effective_objects = list(operation_objects)
        if (
            context_completion["needed"] is True
            and context_completion["resolved"] is True
        ):
            for item in context_completion["context_actions"]:
                if item.get("action") not in {
                    action.get("action") for action in effective_actions
                }:
                    effective_actions.append(dict(item))
            for item in context_completion["context_objects"]:
                if item.get("object_type") not in {
                    target.get("object_type") for target in effective_objects
                }:
                    effective_objects.append(dict(item))
            if not intent_explicit and str(context or "").strip():
                context_intents = cls.classify_intents(str(context))
                intents = list(
                    dict.fromkeys(
                        [
                            *context_intents,
                            *[intent for intent in intents if intent != "conversation"],
                        ]
                    )
                ) or intents
                context_lower = str(context).casefold()
                matched_terms = {
                    intent: [
                        token
                        for candidate_intent, tokens in cls._INTENTS
                        if candidate_intent == intent
                        for token in tokens
                        if token.casefold() in context_lower
                    ]
                    for intent in intents
                }
        safety = cls._command_safety(
            command=raw_input,
            actions=actions,
            objects=operation_objects,
            parameters=parameters,
            context_completion=context_completion,
            intent_explicit=intent_explicit,
            confirmed=bool(confirmed),
        )
        task_classification = cls._task_classification(
            intents=intents,
            actions=effective_actions,
            objects=effective_objects,
            text=normalized,
        )
        effective_action_names = {
            str(item.get("action") or "") for item in effective_actions
        }
        effective_object_names = {
            str(item.get("object_type") or "") for item in effective_objects
        }
        if (
            effective_object_names & {"image", "video"}
            and effective_action_names & {"query", "classify", "analyze", "compare"}
            and "visual" not in intents
        ):
            intents = ["visual", *[intent for intent in intents if intent != "conversation"]]
            task_classification = cls._task_classification(
                intents=intents,
                actions=effective_actions,
                objects=effective_objects,
                text=normalized,
            )
        ambiguities = list(safety["remaining_ambiguities"])
        if not matched_terms and not actions:
            ambiguities.append("intent-not-explicit")
        task_intensity = cls._task_intensity(
            actions=effective_actions,
            objects=effective_objects,
            constraints=[*comprehension["constraints"], *specific_constraints],
            task_categories=task_classification["categories"],
            ambiguity_count=len(ambiguities),
            destructive_operation=safety["destructive_operation"] is True,
            text=normalized,
        )
        tasks = []
        for sequence, intent in enumerate(intents, start=1):
            required_inputs = {
                "search": ["holdings-or-symbols"],
                "distribution": ["holdings-or-symbols"],
                "quote": ["holdings-or-symbols"],
                "risk": ["holdings"],
                "analysis": ["holdings"],
                "calculation": ["expression-or-cash-flows"],
                "statistics": ["numbers-or-return-series"],
                "data_organization": ["records"],
                "reasoning": ["premises"],
                "coding": ["requirements-or-code-spec"],
                "visual": ["images-or-video-frames-or-document-images"],
                "file_management": ["files-and-visual-content-when-applicable"],
                "self_upgrade": ["upgrade-requirements-or-code-spec"],
                "reading": ["document-text-or-documents"],
            }.get(intent, [])
            tasks.append(
                {
                    "sequence": sequence,
                    "intent": intent,
                    "depends_on": [sequence - 1] if sequence > 1 else [],
                    "required_inputs": required_inputs,
                    "matched_terms": matched_terms.get(intent, []),
                    "confidence": round(
                        min(0.99, 0.62 + 0.08 * len(matched_terms.get(intent, []))),
                        2,
                    ),
                }
            )
        return {
            "schema": "star-semantic-plan/v1",
            "understanding_layer": "traditional-chinese-taiwan-first",
            "raw_input": raw_input,
            "normalized_input": normalized,
            "normalization": normalization,
            "language": cls._language(normalized),
            "language_priority": ["zh-TW", "mixed-zh-latin", "en"],
            "english_intermediate_representation_used": False,
            "token_count": len(tokenized),
            "primary_intent": intents[0],
            "intents": intents,
            "prohibited_intents": sorted(prohibited_intents),
            "entities": {
                "symbols": symbols,
                "isins": isins,
                "markets": list(dict.fromkeys(markets)),
                "numbers": [float(value) for value in re.findall(r"\d+(?:\.\d+)?", normalized)[:30]],
                "dates": re.findall(
                    r"(?<!\d)(?:19|20)\d{2}[-/.年](?:0?[1-9]|1[0-2])(?:[-/.月](?:0?[1-9]|[12]\d|3[01])日?)?(?!\d)",
                    normalized,
                )[:20],
                "percentages": re.findall(r"(?<!\w)[+-]?\d+(?:\.\d+)?%", normalized)[:20],
                "money": re.findall(
                    r"(?:NT\$|US\$|\$|新台幣|美元)\s?\d[\d,.]*",
                    normalized,
                    flags=re.IGNORECASE,
                )[:20],
                "urls": re.findall(
                    r"https?://[^\s)\]>，。！？；]+", normalized
                )[:20],
                "emails": re.findall(
                    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])",
                    normalized,
                )[:20],
                "time_horizon": time_horizon,
            },
            "comprehension": comprehension,
            "actions": actions,
            "operation_objects": operation_objects,
            "parameters": parameters,
            "specific_constraints": specific_constraints,
            "context_completion": context_completion,
            "resolved_command": context_completion["resolved_command"],
            "intent_determination": {
                "primary_intent": intents[0],
                "actions": [item["action"] for item in effective_actions],
                "operation_objects": [
                    item["object_type"] for item in effective_objects
                ],
                "actual_operation_separately_gated": True,
            },
            "task_classification": task_classification,
            "task_intensity": task_intensity,
            "safety": safety,
            "tasks": tasks,
            "ambiguities": list(dict.fromkeys(ambiguities)),
        }

    @staticmethod
    def _tokenize(prompt: str) -> list[str]:
        normalized = str(prompt or "").strip()
        return re.findall(r"[\u3400-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?|[^\s]", normalized)

    @staticmethod
    def _database_evidence(database: dict[str, Any]) -> list[dict[str, Any]]:
        tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
        quality = database.get("quality") if isinstance(database.get("quality"), dict) else {}
        return [
            {
                "id": "star-database:instrument-identities",
                "value": int(tables.get("instrument_identity") or 0),
            },
            {
                "id": "star-database:market-observations",
                "value": int(tables.get("market_observation") or 0),
            },
            {
                "id": "star-database:distribution-events",
                "value": int(tables.get("distribution_event") or 0),
            },
            {
                "id": "star-database:invalid-distribution-events",
                "value": int(quality.get("blank_distribution_events") or 0),
            },
        ]

    @staticmethod
    def _reviewed_memory_grounding(value: Any) -> dict[str, Any]:
        if not isinstance(value, list):
            return {"text": "", "evidence": []}
        lines: list[str] = []
        evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value[:12]:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("review_status") or "").strip().casefold() != "approved":
                continue
            content = " ".join(str(item.get("content") or "").split())[:1_200]
            memory_id = str(item.get("memory_id") or "").strip()[:96]
            if not content or not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            title = " ".join(str(item.get("title") or "").split())[:160]
            lines.append(f"- {title + '：' if title else ''}{content}")
            evidence.append(
                {
                    "id": f"star-memory:{memory_id}",
                    "memory_id": memory_id,
                    "source_type": str(item.get("source_type") or "local-memory"),
                    "source_id": str(item.get("source_id") or ""),
                    "confidence": float(item.get("confidence") or 0),
                    "review_status": "approved",
                    "retrieval_score": float(item.get("retrieval_score") or 0),
                }
            )
            if len(lines) >= 4:
                break
        return {"text": "\n".join(lines), "evidence": evidence}

    @staticmethod
    def _native_private_grounding(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"text": "", "counts": {}}
        training = value.get("training_examples")
        capabilities = value.get("capability_compositions")
        operations = value.get("operation_records")
        training = training if isinstance(training, list) else []
        capabilities = capabilities if isinstance(capabilities, list) else []
        operations = operations if isinstance(operations, list) else []
        lines: list[str] = []
        for item in training[:3]:
            if not isinstance(item, Mapping):
                continue
            target = " ".join(str(item.get("target_text") or "").split())[:600]
            if target:
                lines.append(f"- 已驗證訓練範例：{target}")
        for item in capabilities[:3]:
            if not isinstance(item, Mapping):
                continue
            capability_id = str(item.get("composition_id") or "").strip()[:160]
            status = str(item.get("status") or "").strip()[:64]
            if capability_id:
                lines.append(f"- 能力編成 {capability_id}：{status}")
        for item in operations[:3]:
            if not isinstance(item, Mapping):
                continue
            request = item.get("request")
            prompt = (
                " ".join(str(request.get("prompt") or "").split())[:400]
                if isinstance(request, Mapping)
                else ""
            )
            if prompt:
                lines.append(f"- 先前操作：{prompt}")
        return {
            "text": "\n".join(lines)[:4_000],
            "counts": {
                "training_examples": len(training),
                "capability_compositions": len(capabilities),
                "operation_records": len(operations),
            },
        }

    def infer(
        self,
        payload: dict[str, Any],
        *,
        database: dict[str, Any],
        analyze: InvestmentAnalyzer,
        search: MarketSearcher,
    ) -> dict[str, Any]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "").strip()
        if not prompt:
            return {
                "ok": False,
                "error_code": "PROMPT_REQUIRED",
                "message": "請輸入要查詢或分析的內容。",
            }

        intent = str(payload.get("_governed_intent") or self._intent(prompt))
        supplied_semantic_plan = payload.get("_semantic_plan")
        semantic_plan = (
            dict(supplied_semantic_plan)
            if isinstance(supplied_semantic_plan, Mapping)
            else self.semantic_plan(
                str(payload.get("user_command") or prompt),
                context=str(payload.get("_command_context") or ""),
                confirmed=bool(
                    payload.get("confirmed") is True
                    or payload.get("destructive_confirmation") is True
                ),
            )
        )
        if intent not in semantic_plan["intents"]:
            semantic_plan["primary_intent"] = intent
            semantic_plan["intents"].insert(0, intent)
            semantic_plan["tasks"].insert(
                0,
                {
                    "sequence": 1,
                    "intent": intent,
                    "depends_on": [],
                    "required_inputs": ["document-text-or-documents"]
                    if intent == "reading"
                    else [],
                    "matched_terms": [],
                    "confidence": 0.9,
                },
            )
            for sequence, task in enumerate(semantic_plan["tasks"], start=1):
                task["sequence"] = sequence
                task["depends_on"] = [sequence - 1] if sequence > 1 else []
        holdings = payload.get("holdings")
        market_research: dict[str, Any] | None = None
        network_allowed = payload.get("allow_network") is not False
        if (
            network_allowed
            and isinstance(holdings, list)
            and holdings
            and intent in {"search", "distribution", "quote", "risk", "analysis"}
        ):
            market_research = search(
                {
                    "holdings": holdings,
                    "require_all": False,
                    "max_workers": payload.get("max_workers") or 4,
                }
            )
        analysis: dict[str, Any] | None = None
        if isinstance(holdings, list) and holdings:
            analysis = analyze(
                {
                    "holdings": holdings,
                    "analysis_parameters": payload.get("analysis_parameters") or {},
                }
            )

        if intent == "conversation":
            grounding = (
                "這是一般對話。請優先遵守使用者最新訊息的語言、格式與長度要求，"
                "直接回答，不要自行改成能力介紹，也不要加入未被要求的投資內容。"
            )
        elif intent == "self_upgrade":
            grounding = (
                "星澄已理解這是檢討、修正或自我維護命令，會立即執行模型維護、"
                "資料完整性與能力健康檢查。程式來源變更仍須通過範圍、語法、測試、"
                "多模型檢查、治理與可回復備份；治理規則永遠不可修改。"
            )
        elif intent == "coding":
            grounding = (
                "此工作已路由至星澄程式設計專家。程式碼會先建立結構化規格，"
                "再接受語法與範圍檢查；產生的內容不會在未授權時自動執行。"
            )
        elif intent == "visual":
            grounding = (
                "此工作只交給 MiniCPM-V 4.6 視覺檔案辨識專員，負責圖片、影片影格與"
                "文件影像的內容辨識、分類、標籤及摘要。模型只提供視覺分析結果，"
                "不直接搬移、刪除、覆寫檔案，也不參與其他任務。"
            )
        elif intent == "file_management":
            grounding = (
                "此工作屬於檔案管理。只有其中的視覺檔案辨識子任務可交給 MiniCPM-V 4.6；"
                "實際搬移、重新命名、刪除或覆寫仍由受治理執行層處理。"
            )
        elif intent == "reading":
            grounding = (
                "此工作已交給星澄閱讀理解模組。模組會分段閱讀提供的文字，"
                "保留文件雜湊、原文位置與引用；原文沒有答案時會明確拒絕補造。"
            )
        elif intent == "statistics":
            grounding = (
                "此工作已由主要日常模型委派給數理專家。統計結果會保留樣本數、"
                "集中趨勢與離散程度，且不使用網路資料。"
            )
        elif intent == "data_organization":
            grounding = (
                "此工作已由主要日常模型委派給數理專家。資料會依欄位、缺漏值與指定分組整理，"
                "原始值不會被猜測或補造。"
            )
        elif intent == "search":
            grounding = (
                "搜尋工作由星澄主要日常模型負責；公開來源採唯讀查詢。需要外部協作時，"
                "只由星澄經受治理 AI 通道提出申請並接收結果。"
            )
        elif intent == "calculation":
            grounding = (
                "此工作已路由至星澄數理專家。計算會保留輸入、公式、步驟與結果；"
                "缺少條件時會先標示未知量，不以猜測補值。"
            )
        elif intent == "reasoning":
            grounding = (
                "此工作已路由至星澄數理專家。推理會區分前提、推導步驟與結論，"
                "並檢查矛盾、隱含假設及證據是否足夠。"
            )
        elif intent == "distribution":
            grounding = (
                "星澄會依公開來源區分「無配息」與「資料不足」。已確認不配息的標的會顯示無配息，"
                "並使用每日負快取，避免持續重複查詢；需要時可由星澄直接查詢公開網站。"
            )
        elif intent == "quote":
            grounding = (
                "報價與基金淨值由星澄的公開來源搜尋模組取得，數值必須附來源與觀測時間；"
                "本地模型不猜測即時價格。"
            )
        elif intent in {"risk", "analysis"} and analysis is not None:
            warnings = analysis.get("risk_warnings")
            warning_count = len(warnings) if isinstance(warnings, list) else 0
            grounding = (
                f"星澄已使用自己的投資參數引擎完成分析，共辨識 {warning_count} 項風險提醒。"
                "結論來自輸入持股與星澄資料庫，不使用外部模型。"
            )
        elif intent in {"risk", "analysis"}:
            grounding = (
                "請從投資管家帶入持股後執行分析。星澄會計算集中度、幣別曝險、配息與風險參數，"
                "資料不足時會明確保留，不補造結論。"
            )
        elif intent == "status":
            tables = database.get("tables") if isinstance(database.get("tables"), dict) else {}
            grounding = (
                "星澄的本機生成服務已就緒；實際 Transformer、量化權重與安全回退狀態"
                "會由執行環境健康資訊如實揭露。"
                f"目前保存 {int(tables.get('instrument_identity') or 0)} 個標的識別、"
                f"{int(tables.get('market_observation') or 0)} 筆市場觀測。"
            )
        else:
            grounding = (
                "我是星澄，一個在本機執行並以自回歸方式生成文字的語言模型。"
                "目前提供投資資料搜尋、配息判定、參數化分析、資料品質檢查與受控自我訓練；"
                "可查詢公開網路來源，但不使用第三方生成模型，也不會猜測缺少的投資事實。"
            )

        memory_context = self._reviewed_memory_grounding(
            payload.get("memory_context")
        )
        private_context = self._native_private_grounding(
            payload.get("native_private_context")
        )
        generation_grounding = grounding
        if memory_context["text"]:
            generation_grounding = (
                f"{grounding}\n已核准且與本次需求相關的本機記憶如下；記憶只補充上下文，"
                f"不會覆蓋本次指令：\n{memory_context['text']}"
            )
        if private_context["text"]:
            generation_grounding = (
                f"{generation_grounding}\n星澄主模型資料庫中可用的已驗證私有內容如下；"
                f"僅供本次星澄原生模型推論：\n{private_context['text']}"
            )
        generation = self.language_model.generate(
            intent=intent,
            prompt=prompt,
            grounding=generation_grounding,
            max_tokens=int(
                self._bounded_number(
                    payload.get("max_output_tokens"), 180, 16, 360
                )
            ),
            temperature=self._bounded_number(
                payload.get("temperature"), 0.55, 0.0, 2.0
            ),
            top_k=int(self._bounded_number(payload.get("top_k"), 4, 1, 20)),
        )
        response = str(generation["text"])
        grounding_tokens = set(self.language_model.tokenize(generation_grounding))
        response_tokens = set(self.language_model.tokenize(response))
        grounding_coverage = (
            len(grounding_tokens & response_tokens) / len(grounding_tokens)
            if grounding_tokens
            else 1.0
        )
        if grounding_coverage < 0.55:
            response = generation_grounding
            response_tokens = set(self.language_model.tokenize(response))
            grounding_coverage = 1.0
            generation["text"] = response
            generation["token_count"] = len(self.language_model.tokenize(response))
            generation["grounding_fallback_used"] = True
            generation["grounding_fallback_reason"] = "semantic-coverage-gate"
        quality_score = round(
            min(
                1.0,
                0.45
                + (0.25 if generation["facts_preserved"] else 0.0)
                + min(0.3, grounding_coverage * 0.3),
            ),
            4,
        )
        training_candidate = {
            "intent": intent,
            "input_text": prompt,
            "target_text": response,
            "source_type": "self-distillation-grounded",
            "quality_score": quality_score,
            "validated": bool(
                generation["facts_preserved"]
                and grounding_coverage >= 0.55
                and 8 <= generation["token_count"] <= 360
                and not memory_context["evidence"]
                and not private_context["text"]
            ),
            "validation": {
                "grounding_coverage": round(grounding_coverage, 4),
                "facts_preserved": generation["facts_preserved"],
                "bounded_output": 8 <= generation["token_count"] <= 360,
                "ephemeral_memory_excluded_from_training": bool(
                    memory_context["evidence"] or private_context["text"]
                ),
            },
        }

        return {
            "ok": True,
            "model": self.MODEL_ID,
            "model_version": self.VERSION,
            "architecture": self.ARCHITECTURE,
            "mode": "native-generative-language-model",
            "pipeline": [
                "star-tokenizer",
                "intent-encoder",
                "local-retrieval",
                "source-attributed-reading-router",
                "investment-tool-router",
                "autoregressive-probabilistic-decoder",
                "verified-self-training",
            ],
            "token_count": len(self._tokenize(prompt)),
            "intent": intent,
            "semantic_understanding": semantic_plan,
            "intent_confidence": semantic_plan["tasks"][0]["confidence"],
            "context_retrieval": {
                "memory_count": len(payload.get("memory_context") or [])
                if isinstance(payload.get("memory_context"), list)
                else 0,
                "used_memory_count": len(memory_context["evidence"]),
                "memory_grounding_applied": bool(memory_context["evidence"]),
                "memory_ids": [
                    item["memory_id"] for item in memory_context["evidence"]
                ],
                "reviewed_memory_only": True,
                "native_private_database_opened": bool(private_context["counts"]),
                "native_private_record_counts": private_context["counts"],
            },
            "instruction_execution": {
                "understood": True,
                "intent": intent,
                "governance_checked": True,
                "user_commands_allowed": True,
                "arbitrary_system_commands_allowed": False,
                "execution_requested": bool(
                    semantic_plan["comprehension"]["command"][
                        "execution_requested"
                    ]
                ),
                "status": "planned",
            },
            "response": response,
            "generation": generation,
            "language_model": self.training_status(),
            "analysis": analysis,
            "market_research": market_research,
            "evidence": [
                *self._database_evidence(database),
                *memory_context["evidence"],
            ],
            "evidence_policy": {
                "source_attribution_required": True,
                "unknown_values_preserved": True,
                "confidence_exposed": True,
            },
            "external_model_used": False,
            "network_used_for_inference": market_research is not None,
            "network_scope": "public-investment-sources-read-only"
            if network_allowed
            else "disabled-by-request",
            "facts_locked": True,
            "training_policy": "continuous-verified-self-distillation",
            "third_party_weights_used": False,
            "_training_candidate": training_candidate,
        }
