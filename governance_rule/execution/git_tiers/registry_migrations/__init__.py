"""Governed schema migration chain for automation registry and merge queue.

``REGISTRY_STEPS`` / ``QUEUE_STEPS`` are the single ordered chain the
migration engine consumes.  Each step records its own ``migration_id`` and
schema pair so the ledger can reconstruct exactly which transformation ran
between an ``input_digest`` and an ``output_digest``.
"""
from __future__ import annotations

from .base import (
    MigrationStep,
    SchemaError,
    canonical_digest,
    deep_copy,
    detect_schema,
)
from .v1_to_v2 import QUEUE_STEP as QUEUE_V1_TO_V2
from .v1_to_v2 import REGISTRY_STEP as REGISTRY_V1_TO_V2
from .v2_to_v3 import QUEUE_STEP as QUEUE_V2_TO_V3
from .v2_to_v3 import REGISTRY_STEP as REGISTRY_V2_TO_V3

REGISTRY_STEPS: tuple[MigrationStep, ...] = (
    REGISTRY_V1_TO_V2,
    REGISTRY_V2_TO_V3,
)
QUEUE_STEPS: tuple[MigrationStep, ...] = (
    QUEUE_V1_TO_V2,
    QUEUE_V2_TO_V3,
)

TARGET_REGISTRY_SCHEMA: int = REGISTRY_STEPS[-1].new_schema
TARGET_QUEUE_SCHEMA: int = QUEUE_STEPS[-1].new_schema
STEPS_BY_TARGET: dict[str, tuple[MigrationStep, ...]] = {
    "registry": REGISTRY_STEPS,
    "queue": QUEUE_STEPS,
}

__all__ = [
    "QUEUE_STEPS",
    "REGISTRY_STEPS",
    "STEPS_BY_TARGET",
    "TARGET_QUEUE_SCHEMA",
    "TARGET_REGISTRY_SCHEMA",
    "MigrationStep",
    "SchemaError",
    "canonical_digest",
    "deep_copy",
    "detect_schema",
]
