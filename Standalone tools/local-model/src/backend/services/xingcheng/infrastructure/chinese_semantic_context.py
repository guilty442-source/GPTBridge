"""Chinese Semantic Engine — intent/context mixin (split from chinese_semantic_engine, A185).

Intent classification, prohibited-surface detection, conversation-context
(ellipsis/reference) completion, mixed-language detection, and destructive-
ambiguity safety checks.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .chinese_semantic_lexicon import (
    ACTION_LEXICON,
    INTENT_LEXICON,
    OBJECT_LEXICON,
    PROHIBITED_SURFACES,
    REFERENCE_MARKERS,
)


class ChineseSemanticContext:
    """Intent, context-completion and safety helpers (mixin)."""

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
