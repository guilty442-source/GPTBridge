from __future__ import annotations

import re
from typing import Any


class StarNativeIntentMixin:
    """Intent classification, language detection, and tokenization helpers."""

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

    @staticmethod
    def _tokenize(prompt: str) -> list[str]:
        normalized = str(prompt or "").strip()
        return re.findall(r"[\u3400-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?|[^\s]", normalized)
