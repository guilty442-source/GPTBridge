"""Chinese Semantic Engine — analysis record (split from chinese_semantic_engine, A185).

``ChineseSemanticAnalysis`` is the closed, immutable result of one
Traditional-Chinese semantic analysis.  The record projects to the
``star-chinese-semantics/v1`` and ``star-semantic-plan/v1`` schemas used by
the governed inference pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .chinese_semantic_lexicon import UNDERSTANDING_FEATURES


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
