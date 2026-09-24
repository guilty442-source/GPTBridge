"""Deterministic comparison of multi-provider replies.

The comparator only reports *how the outputs differ* — common points,
divergences, contradictions and unanswered questions.  Majority agreement
is never treated as proof of correctness.
"""

from __future__ import annotations

import re
from typing import Any

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NEGATION = re.compile(r"(不|沒有|非|否|勿|別|never|not|no\s|cannot|don't)")
_QUESTION = re.compile(r"[？?]$")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


def _statements(text: str) -> list[str]:
    out: list[str] = []
    for raw in _SENTENCE_SPLIT.split(str(text or "")):
        item = raw.strip().strip("-*• \t")
        if len(item) >= 4:
            out.append(item)
    return out


def _key(sentence: str) -> str:
    return _normalize(sentence)


class CollaborationResultComparator:
    """Compare all completed provider replies of one task."""

    def compare(
        self,
        task_id: str,
        responses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        completed = [
            item
            for item in responses
            if str(item.get("response_status") or "") == "completed"
            and str(item.get("response_text") or "").strip()
        ]
        per_provider: dict[str, list[str]] = {}
        for item in completed:
            provider = str(item.get("provider_id") or "")
            per_provider[provider] = _statements(
                str(item.get("response_text") or "")
            )

        # Statement → providers that said it (normalized comparison).
        index: dict[str, set[str]] = {}
        originals: dict[str, str] = {}
        for provider, statements in per_provider.items():
            for statement in statements:
                key = _key(statement)
                index.setdefault(key, set()).add(provider)
                originals.setdefault(key, statement)

        provider_ids = set(per_provider)
        common = [
            {
                "text": originals[key],
                "sources": sorted(sources),
            }
            for key, sources in index.items()
            if len(sources) > 1
        ]
        differences = [
            {
                "text": originals[key],
                "source_provider": next(iter(sources)),
            }
            for key, sources in index.items()
            if len(sources) == 1
        ]
        contradictions = self._contradictions(completed)
        unanswered = self._unanswered(completed)

        return {
            "task_id": task_id,
            "individual_responses": [
                {
                    "provider_id": str(item.get("provider_id") or ""),
                    "response_id": str(item.get("response_id") or ""),
                    "response_text": str(item.get("response_text") or ""),
                }
                for item in completed
            ],
            "common_points": common,
            "differences": differences,
            "contradictions": contradictions,
            "unanswered_questions": unanswered,
            "providers_compared": sorted(provider_ids),
            "note": (
                "Comparison describes output similarity only; "
                "agreement between models is not evidence of correctness."
            ),
        }

    @staticmethod
    def _contradictions(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flag statement pairs that share numbers/entities but disagree
        on polarity — a heuristic signal, not a verdict."""
        findings: list[dict[str, Any]] = []
        per_provider = {
            str(item.get("provider_id") or ""): _statements(
                str(item.get("response_text") or "")
            )
            for item in responses
        }
        providers = sorted(per_provider)
        for i, left_id in enumerate(providers):
            for right_id in providers[i + 1 :]:
                for left in per_provider[left_id]:
                    left_numbers = set(_NUMBER.findall(left))
                    if not left_numbers:
                        continue
                    for right in per_provider[right_id]:
                        if set(_NUMBER.findall(right)) != left_numbers:
                            continue
                        if bool(_NEGATION.search(_normalize(left))) != bool(
                            _NEGATION.search(_normalize(right))
                        ):
                            findings.append(
                                {
                                    "topic": sorted(left_numbers)[:3],
                                    "statements": [
                                        {"provider_id": left_id, "text": left},
                                        {"provider_id": right_id, "text": right},
                                    ],
                                }
                            )
                            break
        return findings[:8]

    @staticmethod
    def _unanswered(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Questions raised by any provider that no other provider
        addressed at all."""
        raised: dict[str, str] = {}
        all_statements: list[tuple[str, str]] = []
        for item in responses:
            provider = str(item.get("provider_id") or "")
            for statement in _statements(str(item.get("response_text") or "")):
                all_statements.append((provider, statement))
                if statement.rstrip().endswith(("？", "?")):
                    raised[_key(statement)] = provider
        answered_keys: set[str] = set()
        for qkey, asker in raised.items():
            q_numbers = set(_NUMBER.findall(qkey))
            for provider, statement in all_statements:
                if provider == asker or _key(statement) == qkey:
                    continue
                if q_numbers and q_numbers & set(_NUMBER.findall(statement)):
                    answered_keys.add(qkey)
                    break
        return [
            {"question": key, "raised_by": provider}
            for key, provider in raised.items()
            if key not in answered_keys
        ][:8]
