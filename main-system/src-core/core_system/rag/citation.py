"""Citation Validation — 引用完整性與格式驗證。

A487: Citations must be verifiable against canonical metadata.
LLM responses with citations are validated deterministically.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.rag.citation")


@dataclass(frozen=True)
class Citation:
    """Structured citation object for LLM responses."""
    citation_id: str              # e.g., "R1", "R2"
    resource_id: str
    chunk_id: str
    locator_id: str               # module:resource format
    character_start: int
    character_end: int
    content_hash: str
    generation_id: str
    retrieval_score: float
    reranker_score: Optional[float] = None
    module_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "resource_id": self.resource_id,
            "chunk_id": self.chunk_id,
            "locator_id": self.locator_id,
            "character_start": self.character_start,
            "character_end": self.character_end,
            "content_hash": self.content_hash,
            "generation_id": self.generation_id,
            "retrieval_score": self.retrieval_score,
            "reranker_score": self.reranker_score,
            "module_id": self.module_id,
        }


@dataclass(frozen=True)
class CitationValidationResult:
    """Result of citation validation."""
    valid: bool
    citation_id: str
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


class CitationValidator:
    """Validates citations in LLM responses against canonical metadata."""

    # Pattern to find [R1], [R2] etc. in text
    CITATION_PATTERN = re.compile(r'\[R(\d+)\]')

    def __init__(self, metadata_authority: Any) -> None:
        self.metadata = metadata_authority  # PostgreSQLMetadataAuthority

    def extract_citations(self, text: str) -> list[str]:
        """Extract citation IDs from text (e.g., ['R1', 'R2'])."""
        return self.CITATION_PATTERN.findall(text)

    async def validate_response(
        self,
        response_text: str,
        citations: dict[str, Citation],
    ) -> tuple[bool, list[CitationValidationResult]]:
        """Validate all citations in a response.

        Args:
            response_text: The LLM response text containing [R1], [R2] etc.
            citations: Dict mapping citation_id (e.g., "R1") to Citation objects

        Returns:
            (all_valid, list of validation results)
        """
        cited_ids = self.extract_citations(response_text)
        results = []

        for cited_id in cited_ids:
            full_id = f"R{cited_id}"
            citation = citations.get(full_id)
            if not citation:
                results.append(CitationValidationResult(
                    valid=False,
                    citation_id=full_id,
                    errors=(f"Citation {full_id} referenced but not provided",),
                ))
                continue

            result = await self._validate_citation(citation)
            results.append(result)

        # Check for unused citations
        for cid in citations:
            if cid not in cited_ids:
                results.append(CitationValidationResult(
                    valid=True,
                    citation_id=cid,
                    warnings=(f"Citation {cid} provided but not referenced in response",),
                ))

        all_valid = all(r.valid for r in results)
        return all_valid, results

    async def _validate_citation(self, citation: Citation) -> CitationValidationResult:
        """Validate single citation against metadata."""
        errors = []
        warnings = []

        # 1. Citation exists in metadata
        meta = await self.metadata.get_resource_metadata(
            module_id=citation.module_id or citation.locator_id.split(":")[0],
            resource_id=citation.resource_id,
        )

        if meta is None:
            errors.append(f"Resource {citation.resource_id} not found in metadata")
            return CitationValidationResult(
                valid=False,
                citation_id=citation.citation_id,
                errors=tuple(errors),
            )

        # 2. Verify generation_id
        meta_gen = meta.get("generation_id")
        if meta_gen and citation.generation_id != meta_gen:
            errors.append(
                f"Generation mismatch: citation={citation.generation_id} "
                f"metadata={meta_gen}"
            )

        # 3. Verify content_hash
        meta_hash = meta.get("content_hash") or meta.get("sha256")
        if meta_hash and citation.content_hash != meta_hash:
            errors.append(
                f"Content hash mismatch: citation={citation.content_hash[:16]}... "
                f"metadata={meta_hash[:16]}..."
            )

        # 4. Verify chunk exists and character range valid
        # In production: fetch chunk and verify character_start/end
        if citation.character_start < 0 or citation.character_end <= citation.character_start:
            errors.append(
                f"Invalid character range: {citation.character_start}-{citation.character_end}"
            )

        # 5. Verify status is INDEXED
        if meta.get("status") != "INDEXED":
            warnings.append(f"Resource status is {meta.get('status')}, not INDEXED")

        # 6. Score sanity checks
        if not 0 <= citation.retrieval_score <= 1:
            warnings.append(f"Retrieval score out of range: {citation.retrieval_score}")
        if citation.reranker_score is not None and not 0 <= citation.reranker_score <= 1:
            warnings.append(f"Reranker score out of range: {citation.reranker_score}")

        return CitationValidationResult(
            valid=len(errors) == 0,
            citation_id=citation.citation_id,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    async def validate_citation_object(self, citation: Citation) -> CitationValidationResult:
        """Validate a single citation object (for programmatic use)."""
        return await self._validate_citation(citation)


class CitationFormatter:
    """Formats citations for LLM context and response."""

    @staticmethod
    def format_for_context(citations: list[Citation]) -> str:
        """Format citations for inclusion in LLM prompt context."""
        lines = ["Sources:"]
        for c in citations:
            lines.append(
                f"[{c.citation_id}] {c.locator_id} "
                f"(chars {c.character_start}-{c.character_end}, "
                f"score={c.retrieval_score:.3f}, gen={c.generation_id})"
            )
        return "\n".join(lines)

    @staticmethod
    def format_for_response(citations: list[Citation]) -> str:
        """Format citations for LLM response footer."""
        if not citations:
            return ""
        ids = ", ".join(f"[{c.citation_id}]" for c in citations)
        return f"\nSources: {ids}"

    @staticmethod
    def parse_citation_ids(text: str) -> list[str]:
        """Parse citation IDs from LLM response text."""
        return re.findall(r'\[R(\d+)\]', text)


def build_citation_from_hit(
    hit: dict[str, Any],
    index_state: Any,
    citation_id: str,
    module_id: str,
) -> Citation:
    """Build Citation from verified hit and index state."""
    payload = hit.get("payload", {})
    return Citation(
        citation_id=citation_id,
        resource_id=payload.get("resource_id", ""),
        chunk_id=payload.get("chunk_id", str(hit.get("id", ""))),
        locator_id=f"{module_id}:{payload.get('resource_id', '')}",
        character_start=payload.get("character_start", 0),
        character_end=payload.get("character_end", 0),
        content_hash=index_state.content_hash,
        generation_id=index_state.generation_id or "",
        retrieval_score=hit.get("score", 0.0),
        reranker_score=hit.get("reranker_score"),
        module_id=module_id,
    )


__all__ = [
    "Citation",
    "CitationValidationResult",
    "CitationValidator",
    "CitationFormatter",
    "build_citation_from_hit",
]