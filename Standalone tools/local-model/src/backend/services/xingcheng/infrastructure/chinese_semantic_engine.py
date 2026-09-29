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

Module split (A185): lexicon data lives in ``chinese_semantic_lexicon.py``,
the closed analysis record in ``chinese_semantic_analysis.py``, extraction
mixins in ``chinese_semantic_extract.py``/``chinese_semantic_context.py``.
All public names remain re-exported here.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

from .chinese_semantic_analysis import ChineseSemanticAnalysis
from .chinese_semantic_context import ChineseSemanticContext
from .chinese_semantic_extract import ChineseSemanticExtractors
from .chinese_semantic_lexicon import (
    ACTION_LEXICON,
    COLLOQUIAL_REPAIRS,
    DESTRUCTIVE_ACTIONS,
    INTENT_LEXICON,
    NEGATION_MARKERS,
    OBJECT_LEXICON,
    PROHIBITED_SURFACES,
    QUESTION_MARKERS,
    SCHEMA,
    STOPWORDS,
    TAIWAN_SYNONYMS,
    UNDERSTANDING_FEATURES,
    _NUMERIC_TOKEN,
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


class ChineseSemanticEngine(ChineseSemanticExtractors, ChineseSemanticContext):
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
        # X2: numeric check on the raw token (digits never casefold) skips
        # the casefold entirely; the length check stays on the folded form
        # because casefolding can change length (e.g. 'ß' -> 'ss').
        counts: Counter[str] = Counter()
        for token in tokens:
            if _NUMERIC_TOKEN.fullmatch(token):
                continue
            folded = token.casefold()
            if len(folded) < 2 or folded in STOPWORDS:
                continue
            counts[folded] += 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return ordered[:20]

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
