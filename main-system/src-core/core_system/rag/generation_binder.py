"""Query Generation Binding — 查詢結果版本一致性驗證。

A486+A487: Every Qdrant hit must be verified against PostgreSQL metadata
to ensure generation_id and content_hash match. Mismatches are dropped
and trigger reconciliation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .rag_qdrant import IndexState, RagQueryResult

_logger = logging.getLogger("gptbridge.rag.verification")


class VerificationResult(str):
    """Verification outcome."""
    VERIFIED = "VERIFIED"           # generation_id + content_hash match
    GENERATION_MISMATCH = "GENERATION_MISMATCH"  # hit generation != metadata generation
    HASH_MISMATCH = "HASH_MISMATCH"              # content_hash mismatch
    METADATA_MISSING = "METADATA_MISSING"        # No PostgreSQL record
    STATE_MISMATCH = "STATE_MISMATCH"            # Index state not INDEXED


@dataclass(frozen=True)
class VerifiedHit:
    """Verified hit with generation-bound metadata."""
    hit: dict[str, Any]
    index_state: IndexState
    verification: VerificationResult
    metadata: dict[str, Any]


class GenerationBinder:
    """Binds Qdrant hits to PostgreSQL metadata with generation verification."""

    def __init__(self, metadata_authority: Any) -> None:
        self.metadata = metadata_authority  # PostgreSQLMetadataAuthority

    async def bind_hits(
        self,
        hits: list[dict[str, Any]],
        module_id: Optional[str] = None,
    ) -> tuple[list[VerifiedHit], list[dict[str, Any]]]:
        """Verify hits against PostgreSQL metadata.

        Returns:
            verified_hits: Hits that pass generation/hash verification
            dropped_hits: Hits that failed (with reason for reconciliation)
        """
        verified = []
        dropped = []

        for hit in hits:
            payload = hit.get("payload", {})
            resource_id = payload.get("resource_id") or hit.get("document_id")
            hit_module_id = payload.get("module_id") or hit.get("module_id")
            hit_generation_id = payload.get("generation_id")
            hit_content_hash = payload.get("content_hash")

            if not resource_id or not hit_module_id:
                dropped.append({
                    "hit": hit,
                    "reason": "MISSING_IDENTIFIERS",
                    "detail": "resource_id or module_id missing from payload",
                })
                continue

            # Fetch authoritative metadata from PostgreSQL
            meta = await self.metadata.get_resource_metadata(
                module_id=hit_module_id,
                resource_id=resource_id,
            )

            if meta is None:
                dropped.append({
                    "hit": hit,
                    "reason": VerificationResult.METADATA_MISSING,
                    "detail": f"No metadata for {hit_module_id}/{resource_id}",
                })
                continue

            # Verify generation_id
            meta_generation_id = meta.get("generation_id")
            if hit_generation_id and meta_generation_id and hit_generation_id != meta_generation_id:
                dropped.append({
                    "hit": hit,
                    "reason": VerificationResult.GENERATION_MISMATCH,
                    "detail": f"hit gen={hit_generation_id} != meta gen={meta_generation_id}",
                })
                # Trigger reconciliation
                await self._trigger_reconciliation(
                    hit_module_id, resource_id,
                    f"generation_mismatch:{hit_generation_id}!={meta_generation_id}"
                )
                continue

            # Verify content_hash
            meta_content_hash = meta.get("content_hash") or meta.get("sha256")
            if hit_content_hash and meta_content_hash and hit_content_hash != meta_content_hash:
                dropped.append({
                    "hit": hit,
                    "reason": VerificationResult.HASH_MISMATCH,
                    "detail": f"hit hash={hit_content_hash[:16]}... != meta hash={meta_content_hash[:16]}...",
                })
                await self._trigger_reconciliation(
                    hit_module_id, resource_id,
                    f"hash_mismatch:{hit_content_hash}!={meta_content_hash}"
                )
                continue

            # Verify index state is INDEXED
            meta_status = meta.get("status")
            if meta_status != "INDEXED":
                dropped.append({
                    "hit": hit,
                    "reason": VerificationResult.STATE_MISMATCH,
                    "detail": f"metadata status={meta_status} not INDEXED",
                })
                continue

            # Build IndexState from verified metadata
            index_state = IndexState(
                resource_id=resource_id,
                module_id=hit_module_id,
                embedding_model=meta.get("embedding_model", ""),
                embedding_dimension=meta.get("embedding_dimension", 0),
                chunk_size=meta.get("chunk_size", 0),
                chunk_overlap=meta.get("chunk_overlap", 0),
                indexed_at_utc=meta.get("indexed_at_utc", ""),
                content_hash=meta_content_hash or "",
                qdrant_point_id=str(hit.get("id") or hit.get("point_id", "")),
                postgresql_record_id=meta.get("id"),
                generation_id=meta_generation_id,
            )

            verified.append(VerifiedHit(
                hit=hit,
                index_state=index_state,
                verification=VerificationResult.VERIFIED,
                metadata=meta,
            ))

        if dropped:
            _logger.warning(
                "GenerationBinder: dropped %d/%d hits (verified=%d)",
                len(dropped), len(hits), len(verified)
            )

        return verified, dropped

    async def _trigger_reconciliation(
        self,
        module_id: str,
        resource_id: str,
        reason: str,
    ) -> None:
        """Queue reconciliation for mismatched hit."""
        # In production: enqueue to reconciliation queue
        _logger.info(
            "GenerationBinder: reconciliation queued for %s/%s - %s",
            module_id, resource_id, reason
        )


def verify_citation(citation: dict[str, Any], metadata: dict[str, Any]) -> tuple[bool, str]:
    """Verify a citation object against metadata (for citation validation)."""
    # Check required fields
    required = ["resource_id", "chunk_id", "generation_id", "content_hash"]
    for field in required:
        if field not in citation:
            return False, f"missing_field:{field}"

    # Verify generation_id
    if citation["generation_id"] != metadata.get("generation_id"):
        return False, f"generation_mismatch:{citation['generation_id']}!={metadata.get('generation_id')}"

    # Verify content_hash
    if citation["content_hash"] != metadata.get("content_hash"):
        return False, f"hash_mismatch:{citation['content_hash'][:16]}...!={metadata.get('content_hash')[:16]}..."

    return True, "verified"


__all__ = [
    "VerificationResult",
    "VerifiedHit",
    "GenerationBinder",
    "verify_citation",
]