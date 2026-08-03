from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..infrastructure.generative_language_model import (
    MIN_TRAINING_GROUNDING_COVERAGE,
)


class StarOllamaTrainingGate:
    """Validates governed local Ollama teaching candidates before Star learns them."""

    MAX_EXAMPLES = 20
    MIN_GROUNDING_COVERAGE = MIN_TRAINING_GROUNDING_COVERAGE
    ALLOWED_INTENTS = frozenset(
        {
            "conversation",
            "capabilities",
            "reading",
            "reasoning",
            "coding",
            "statistics",
            "data_organization",
            "calculation",
            "analysis",
            "risk",
            "self_upgrade",
        }
    )
    _PROHIBITED_PATTERNS = (
        r"ignore\s+(?:all\s+)?previous",
        r"忽略(?:先前|之前|以上).{0,8}(?:指示|規則|提示)",
        r"system\s+prompt",
        r"系統提示詞",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\b(?:api[_ -]?key|access[_ -]?token|password|密碼)\s*[:=]",
    )
    _FACT_PATTERNS = {
        "numbers": r"(?<![A-Za-z])\d+(?:\.\d+)?%?",
        "dates": r"(?<!\d)(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?",
        "money": r"(?:NT\$|US\$|\$|新台幣|美元)\s?\d[\d,.]*",
        "urls": r"https?://[^\s)\]>，。！？；]+",
        "emails": r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])",
    }

    @staticmethod
    def parse_response(content: str) -> list[dict[str, Any]]:
        candidate = str(content or "").strip()
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", candidate)
            if match is None:
                return []
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []
        if isinstance(parsed, dict):
            parsed = parsed.get("examples")
        if not isinstance(parsed, list):
            return []
        return [dict(item) for item in parsed if isinstance(item, dict)][
            : StarOllamaTrainingGate.MAX_EXAMPLES
        ]

    @staticmethod
    def _terms(text: str) -> set[str]:
        normalized = str(text or "").casefold()
        latin = re.findall(r"[a-z][a-z0-9_-]{1,}", normalized)
        chinese = [
            run[index : index + 2]
            for run in re.findall(r"[\u3400-\u9fff]{2,}", normalized)
            for index in range(len(run) - 1)
        ]
        return set(latin + chinese)

    @classmethod
    def _fact_values(cls, text: str) -> dict[str, set[str]]:
        return {
            name: {match.casefold() for match in re.findall(pattern, str(text or ""), re.IGNORECASE)}
            for name, pattern in cls._FACT_PATTERNS.items()
        }

    @classmethod
    def evaluate(
        cls,
        examples: list[dict[str, Any]],
        *,
        requested_intent: str,
        reference_text: str = "",
        response_digest: str = "",
        source_type: str = "ollama-governed-training-candidate",
        received_via: str = "ollama-loopback-only",
    ) -> dict[str, Any]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reference = str(reference_text or "").strip()[:64_000]
        for index, example in enumerate(examples[: cls.MAX_EXAMPLES], start=1):
            intent = str(example.get("intent") or requested_intent).strip().casefold()
            input_text = str(example.get("input_text") or "").strip()[:16_000]
            target_text = str(example.get("target_text") or "").strip()[:16_000]
            reasons: list[str] = []
            if intent not in cls.ALLOWED_INTENTS:
                reasons.append("unsupported-intent")
            if not 4 <= len(input_text) <= 8_000:
                reasons.append("input-length-out-of-range")
            if not 8 <= len(target_text) <= 8_000:
                reasons.append("target-length-out-of-range")
            combined = f"{input_text}\n{target_text}"
            if any(
                re.search(pattern, combined, flags=re.IGNORECASE)
                for pattern in cls._PROHIBITED_PATTERNS
            ):
                reasons.append("prohibited-or-sensitive-content")

            grounding = f"{input_text}\n{reference}".strip()
            grounding_terms = cls._terms(grounding)
            target_terms = cls._terms(target_text)
            coverage = (
                len(grounding_terms & target_terms) / len(target_terms)
                if target_terms
                else 0.0
            )
            if coverage < cls.MIN_GROUNDING_COVERAGE:
                reasons.append("semantic-grounding-too-low")

            grounding_facts = cls._fact_values(grounding)
            target_facts = cls._fact_values(target_text)
            unsupported_facts = {
                name: sorted(values - grounding_facts[name])
                for name, values in target_facts.items()
                if values - grounding_facts[name]
            }
            if unsupported_facts:
                reasons.append("unsupported-facts")

            candidate_id = str(example.get("candidate_id") or f"ollama-example-{index}")[:96]
            audit = {
                "candidate_id": candidate_id,
                "intent": intent,
                "semantic_grounding": round(coverage, 4),
                "unsupported_facts": unsupported_facts,
                "response_digest": response_digest,
                "direct_external_write": False,
                "reviewed_by": "star-main-native-model",
                "received_via": str(received_via or "governance-authenticated-ai-channel"),
            }
            if reasons:
                rejected.append({**audit, "reasons": reasons})
                continue
            quality_score = round(min(0.99, 0.82 + coverage * 0.14 + (0.03 if reference else 0)), 4)
            accepted.append(
                {
                    "intent": intent,
                    "input_text": input_text,
                    "target_text": target_text,
                    "source_type": str(source_type or "ollama-governed-training-candidate"),
                    "quality_score": quality_score,
                    "validated": True,
                    "validation": {
                        **audit,
                        "quality_gate": "star-gpt-training-gate/v1",
                        "reference_grounded": bool(reference),
                        "fact_preservation_verified": True,
                        # Canonical fields are shared with self-distillation and
                        # model maintenance.  Keeping one validation contract
                        # prevents accepted GPT examples from being deactivated
                        # during the next maintenance cycle.
                        "grounding_coverage": round(coverage, 4),
                        "facts_preserved": not unsupported_facts,
                        "bounded_output": 8 <= len(target_text) <= 8_000,
                    },
                }
            )
        return {
            "accepted": accepted,
            "rejected": rejected,
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "response_digest": response_digest,
            "minimum_grounding_coverage": cls.MIN_GROUNDING_COVERAGE,
        }

    @staticmethod
    def digest(content: str) -> str:
        return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


__all__ = ["StarOllamaTrainingGate"]
