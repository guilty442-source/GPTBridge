"""RagSystemManifest — emitted at startup; the answer to "which
architecture version produced this answer?" without digging Git.

Used by debug, audit, benchmark, rollback and migration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .schema_versions import RagSchemaVersions


@dataclass(frozen=True, slots=True)
class RagSystemManifest:
    architecture_version: str
    policy_version: str
    schema: RagSchemaVersions

    active_generation: str
    embedding_model: str
    embedding_dimension: int

    reranker_model: str = ""
    qdrant_version: str = ""
    collection_alias: str = ""
    postgres_schema_version: int = 0

    sub_architectures: tuple[str, ...] = (
        "hybrid-rag", "code-rag", "memory-rag", "agentic-rag",
    )
    canonical_state: str = "STARTING"
    degraded_backend: str = "sqlite-bounded-fallback"

    build_commit: str = ""
    created_at: float = 0.0


def build_manifest(
    *,
    architecture_version: str,
    policy_version: str,
    schema: RagSchemaVersions,
    active_generation: str,
    embedding_model: str,
    embedding_dimension: int,
    **kw: object,
) -> RagSystemManifest:
    return RagSystemManifest(
        architecture_version=architecture_version,
        policy_version=policy_version,
        schema=schema,
        active_generation=active_generation,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        created_at=time.time(),
        **kw,  # type: ignore[arg-type]
    )


__all__ = ["RagSystemManifest", "build_manifest"]
