"""Citation validation — every RAG result must be cited and verifiable (A549).

A549 forbids uncited results and requires citation validation before a
result is returned.  A citation is valid only when it resolves to a
retrieved, verified evidence hit with the same resource, chunk, locator,
content hash and generation identity; any mismatch fails the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .rag_contracts import Citation, RagSearchHit


class CitationVerdict(str, Enum):
    """Closed verdict set for one answer's citations."""

    VALID = "valid"
    INVALID = "invalid"
    UNCITED = "uncited"


@dataclass(frozen=True, slots=True)
class CitationValidationResult:
    """Verdict plus per-citation disposition and reasons."""

    verdict: CitationVerdict
    validated: tuple[Citation, ...] = ()
    invalid: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict is CitationVerdict.VALID

    def to_record(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "validated": [citation.citation_id for citation in self.validated],
            "invalid": list(self.invalid),
            "issues": list(self.issues),
        }


class CitationValidator:
    """Validates answer citations against the retrieved evidence set."""

    def __init__(self, *, require_verified_hits: bool = True) -> None:
        self._require_verified_hits = bool(require_verified_hits)

    def validate(
        self,
        *,
        citations: Sequence[Citation],
        evidence: Sequence[RagSearchHit],
        generation_id: str = "",
    ) -> CitationValidationResult:
        if not citations:
            return CitationValidationResult(
                verdict=CitationVerdict.UNCITED,
                issues=("uncited-result",),
            )

        hits = list(evidence)
        validated: list[Citation] = []
        invalid: list[str] = []
        issues: list[str] = []

        for citation in citations:
            hit = _match_hit(citation, hits)
            if hit is None:
                invalid.append(citation.citation_id)
                issues.append(f"citation-not-in-evidence:{citation.citation_id}")
                continue
            if self._require_verified_hits and not hit.verified:
                invalid.append(citation.citation_id)
                issues.append(f"citation-unverified:{citation.citation_id}")
                continue
            if generation_id and hit.generation_id != generation_id:
                invalid.append(citation.citation_id)
                issues.append(f"citation-generation-mismatch:{citation.citation_id}")
                continue
            if citation.generation_id and citation.generation_id != hit.generation_id:
                invalid.append(citation.citation_id)
                issues.append(f"citation-generation-mismatch:{citation.citation_id}")
                continue
            validated.append(citation)

        if invalid:
            return CitationValidationResult(
                verdict=CitationVerdict.INVALID,
                validated=tuple(validated),
                invalid=tuple(invalid),
                issues=tuple(issues),
            )
        return CitationValidationResult(
            verdict=CitationVerdict.VALID, validated=tuple(validated)
        )


def _match_hit(citation: Citation, hits: Sequence[RagSearchHit]) -> RagSearchHit | None:
    for hit in hits:
        if (
            hit.resource_id == citation.resource_id
            and hit.chunk_id == citation.chunk_id
            and hit.locator_id == citation.locator_id
            and hit.content_hash == citation.content_hash
        ):
            return hit
    return None


__all__ = [
    "CitationValidationResult",
    "CitationValidator",
    "CitationVerdict",
]
