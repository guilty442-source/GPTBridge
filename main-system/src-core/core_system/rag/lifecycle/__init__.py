"""RAG lifecycle — schema versions, generations, gates, deletion,
startup, takeover, DR, manifest, upgrade plan.

The full lifecycle: design -> run -> upgrade -> migrate -> degrade
-> recover -> delete -> validate -> rollback.
"""
from .deletion import (
    DeletionRecord,
    DeletionState,
    DeletionStep,
    DerivedRelation,
    MemoryDeletionRequest,
    MemoryDeletionScope,
    ProvenanceEdge,
    advance_deletion,
    cascade_invalidation,
    query_barrier,
    validate_memory_deletion,
)
from .dr import (
    BACKUP_PRIORITIES,
    REBUILD_SEQUENCE,
    RebuildReport,
    RebuildStep,
    RecoveryTargets,
    evaluate_rebuild,
)
from .gates import (
    AcceptanceThresholds,
    BenchmarkOutcome,
    GateVerdict,
    REQUIRED_SECURITY_PROBES,
    SecurityProbe,
    benchmark_gate,
    deployment_gate,
    security_gate,
)
from .generation import (
    CollectionGeneration,
    GenerationFingerprint,
    GenerationManager,
    GenerationState,
)
from .manifest import RagSystemManifest, build_manifest
from .schema_versions import (
    MigrationPhase,
    MigrationPlan,
    RagSchemaVersions,
    compatible_with,
    plan_migration,
)
from .startup import (
    StartupChecks,
    StartupVerdict,
    TAKEOVER_CRITERIA,
    TakeoverReport,
    evaluate_takeover,
    select_backend,
    startup_gate,
)
from .upgrade_plan import (
    PHASE_ORDER,
    ShadowQueryReport,
    UpgradePhase,
    evaluate_shadow,
    phase_allowed,
)

__all__ = [
    "AcceptanceThresholds",
    "BACKUP_PRIORITIES",
    "BenchmarkOutcome",
    "CollectionGeneration",
    "DeletionRecord",
    "DeletionState",
    "DeletionStep",
    "DerivedRelation",
    "GateVerdict",
    "GenerationFingerprint",
    "GenerationManager",
    "GenerationState",
    "MemoryDeletionRequest",
    "MemoryDeletionScope",
    "MigrationPhase",
    "MigrationPlan",
    "PHASE_ORDER",
    "ProvenanceEdge",
    "REBUILD_SEQUENCE",
    "REQUIRED_SECURITY_PROBES",
    "RagSchemaVersions",
    "RagSystemManifest",
    "RebuildReport",
    "RebuildStep",
    "RecoveryTargets",
    "SecurityProbe",
    "ShadowQueryReport",
    "StartupChecks",
    "StartupVerdict",
    "TAKEOVER_CRITERIA",
    "TakeoverReport",
    "UpgradePhase",
    "advance_deletion",
    "benchmark_gate",
    "build_manifest",
    "cascade_invalidation",
    "compatible_with",
    "deployment_gate",
    "evaluate_rebuild",
    "evaluate_shadow",
    "evaluate_takeover",
    "phase_allowed",
    "plan_migration",
    "query_barrier",
    "security_gate",
    "select_backend",
    "startup_gate",
    "validate_memory_deletion",
]
