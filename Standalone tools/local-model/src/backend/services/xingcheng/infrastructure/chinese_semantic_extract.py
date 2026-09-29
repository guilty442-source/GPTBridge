"""Chinese Semantic Engine — extraction mixin (split from chinese_semantic_engine, A185).

Action/object extraction, entity patterns (date/percentage/money/URL/email/
version/path/model/market/symbol/ISIN), and marker-driven extraction for
negations, questions, constraints, and requested outputs.
"""

from __future__ import annotations

import re
from typing import Any

from .chinese_semantic_lexicon import (
    ACTION_LEXICON,
    CONSTRAINT_MARKERS,
    MARKET_ALIASES,
    NEGATION_MARKERS,
    OBJECT_LEXICON,
    QUESTION_MARKERS,
    REQUESTED_OUTPUT_MARKERS,
)


class ChineseSemanticExtractors:
    """Marker- and pattern-driven extraction helpers (mixin)."""

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
