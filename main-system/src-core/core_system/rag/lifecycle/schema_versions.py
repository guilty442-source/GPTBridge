"""Three-axis schema versioning + governed migration plan.

PG field changes, Qdrant payload changes and RAG behaviour changes
do not move together, so one ``version`` is wrong:

    rag_schema_version      — behaviour/contracts
    metadata_schema_version — PostgreSQL side
    vector_schema_version   — Qdrant payload/point side

Migration is a planned sequence, never a silent runtime mutation:

    detect current -> plan -> apply metadata migration
    -> build new vector generation if required -> validate -> activate
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True, slots=True)
class RagSchemaVersions:
    rag_schema_version: int
    metadata_schema_version: int
    vector_schema_version: int


class MigrationPhase(str, Enum):
    DETECT = "detect"
    PLAN = "plan"
    APPLY_METADATA = "apply_metadata"
    BUILD_VECTOR_GENERATION = "build_vector_generation"
    VALIDATE = "validate"
    ACTIVATE = "activate"


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    current: RagSchemaVersions
    target: RagSchemaVersions
    phases: tuple[MigrationPhase, ...]
    requires_vector_rebuild: bool
    steps: tuple[str, ...] = ()


def plan_migration(
    current: RagSchemaVersions, target: RagSchemaVersions
) -> MigrationPlan:
    """Decide the migration phases — only axes that changed need work."""
    phases: list[MigrationPhase] = [MigrationPhase.DETECT, MigrationPhase.PLAN]
    steps: list[str] = []
    if target.metadata_schema_version != current.metadata_schema_version:
        phases.append(MigrationPhase.APPLY_METADATA)
        steps.append(
            f"metadata {current.metadata_schema_version}->"
            f"{target.metadata_schema_version}"
        )
    rebuild = target.vector_schema_version != current.vector_schema_version
    if rebuild:
        phases.append(MigrationPhase.BUILD_VECTOR_GENERATION)
        steps.append(
            f"vector {current.vector_schema_version}->"
            f"{target.vector_schema_version} (new generation)"
        )
    phases += [MigrationPhase.VALIDATE, MigrationPhase.ACTIVATE]
    return MigrationPlan(
        current=current,
        target=target,
        phases=tuple(phases),
        requires_vector_rebuild=rebuild,
        steps=tuple(steps),
    )


def compatible_with(
    running: RagSchemaVersions, required: RagSchemaVersions
) -> tuple[bool, str]:
    """Startup compatibility check — a newer DB schema than the
    running code expects is BLOCKED, not silently upgraded."""
    if running.metadata_schema_version > required.metadata_schema_version:
        return False, "metadata-schema-newer-than-runtime"
    if running.vector_schema_version > required.vector_schema_version:
        return False, "vector-schema-newer-than-runtime"
    if running.rag_schema_version > required.rag_schema_version:
        return False, "rag-schema-newer-than-runtime"
    return True, "compatible"


__all__ = [
    "MigrationPhase",
    "MigrationPlan",
    "RagSchemaVersions",
    "compatible_with",
    "plan_migration",
]
