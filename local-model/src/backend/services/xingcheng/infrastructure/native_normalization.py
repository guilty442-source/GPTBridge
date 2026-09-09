from __future__ import annotations

import re
from collections import Counter
from typing import Any


class StarNativeNormalizationMixin:
    """Chinese semantic normalization and command-parameter extraction."""

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
