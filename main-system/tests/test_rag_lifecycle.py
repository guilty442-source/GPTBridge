"""Tests for the RAG lifecycle layer.

Covers:
- Three-axis schema versions + migration plan
- Generation shadow build / alias swap / fingerprint reuse
- Benchmark + security deployment gates
- Tombstone-first deletion + cascade + memory scopes
- Startup gate (BLOCKED vs DEGRADED vs CANONICAL)
- Takeover 12-criteria + irreversible invariant
- DR rebuild-from-zero + backup priorities
- Manifest + five-phase upgrade plan + shadow query
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "main-system" / "src-core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core_system.rag.lifecycle import (
    BACKUP_PRIORITIES,
    BenchmarkOutcome,
    DeletionRecord,
    DeletionState,
    DeletionStep,
    DerivedRelation,
    GateVerdict,
    GenerationFingerprint,
    GenerationManager,
    GenerationState,
    MemoryDeletionRequest,
    MemoryDeletionScope,
    MigrationPhase,
    MigrationReport,
    ProvenanceEdge,
    REBUILD_SEQUENCE,
    RagSchemaVersions,
    RebuildReport,
    RebuildStep,
    StartupChecks,
    StartupVerdict,
    TAKEOVER_CRITERIA,
    UpgradePhase,
    advance_deletion,
    apply_migration,
    benchmark_gate,
    cascade_invalidation,
    compatible_with,
    deployment_gate,
    evaluate_rebuild,
    evaluate_shadow,
    evaluate_takeover,
    phase_allowed,
    plan_migration,
    query_barrier,
    security_gate,
    select_backend,
    startup_gate,
    validate_memory_deletion,
    SecurityProbe,
    ShadowQueryReport,
)


# ---------- schema versions ----------

def test_three_axis_versions_independent():
    cur = RagSchemaVersions(4, 7, 3)
    tgt = RagSchemaVersions(4, 8, 3)   # metadata only
    plan = plan_migration(cur, tgt)
    assert not plan.requires_vector_rebuild
    assert MigrationPhase.APPLY_METADATA in plan.phases
    assert MigrationPhase.BUILD_VECTOR_GENERATION not in plan.phases


def test_vector_change_requires_generation():
    plan = plan_migration(
        RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 7, 4),
    )
    assert plan.requires_vector_rebuild
    assert MigrationPhase.BUILD_VECTOR_GENERATION in plan.phases
    # validate + activate always last
    assert plan.phases[-2:] == (MigrationPhase.VALIDATE, MigrationPhase.ACTIVATE)


def test_newer_db_than_runtime_blocked():
    ok, reason = compatible_with(
        RagSchemaVersions(4, 9, 3), RagSchemaVersions(4, 8, 3),
    )
    assert not ok
    assert "metadata" in reason


# ---------- generations ----------

def _fp(model="m", dim=2560, vs=3):
    return GenerationFingerprint(
        parser_version="p1", chunk_policy_version="c1",
        embedding_model=model, embedding_dimension=dim,
        vector_schema_version=vs,
    )


def test_shadow_build_keeps_active():
    gm = GenerationManager()
    g1 = gm.create_generation(_fp())
    gm.mark_validating(g1.generation_id)
    gm.activate(g1.generation_id)
    assert gm.active().generation_id == g1.generation_id

    g2 = gm.create_generation(_fp(model="new", dim=1024))
    assert gm.active().generation_id == g1.generation_id  # g2 shadow
    gm.mark_validating(g2.generation_id)
    gm.mark_shadow(g2.generation_id)
    gm.activate(g2.generation_id)
    assert gm.active().generation_id == g2.generation_id
    assert gm.get(g1.generation_id).state is GenerationState.RETIRED


def test_fingerprint_reuse():
    gm = GenerationManager()
    g1 = gm.create_generation(_fp())
    gm.mark_validating(g1.generation_id)
    gm.activate(g1.generation_id)
    assert gm.find_reusable(_fp()).generation_id == g1.generation_id
    assert gm.find_reusable(_fp(model="other")) is None


def test_illegal_generation_transition():
    gm = GenerationManager()
    g = gm.create_generation(_fp())
    with pytest.raises(ValueError):
        gm.activate(g.generation_id)  # BUILDING can't activate


# ---------- gates ----------

def _outcome(**kw):
    base = dict(recall_at_5=0.8, recall_at_10=0.9, mrr=0.7,
                ndcg_at_10=0.75, zero_result_rate=0.02,
                wrong_hit_rate=0.01, p95_latency_ms=120.0,
                citation_accuracy=0.99)
    base.update(kw)
    return BenchmarkOutcome(**base)


def _probes(passed=True):
    from core_system.rag.lifecycle import REQUIRED_SECURITY_PROBES
    return [SecurityProbe(n, passed) for n in REQUIRED_SECURITY_PROBES]


def test_benchmark_gate_pass():
    verdict, fails = benchmark_gate(_outcome())
    assert verdict is GateVerdict.PASS and not fails


def test_benchmark_gate_fail():
    verdict, fails = benchmark_gate(_outcome(recall_at_5=0.3, mrr=0.1))
    assert verdict is GateVerdict.FAIL
    assert len(fails) >= 2


def test_security_gate_blocks():
    probes = _probes()
    probes[3] = SecurityProbe("tombstoned-resource-searchable", False, "still found")
    verdict, fails = security_gate(probes)
    assert verdict is GateVerdict.DEPLOYMENT_BLOCKED


def test_security_gate_missing_probe_blocks():
    verdict, fails = security_gate(_probes()[:-1])
    assert verdict is GateVerdict.DEPLOYMENT_BLOCKED
    assert any("missing-probe" in f for f in fails)


def test_deployment_gate_security_dominates():
    verdict, _ = deployment_gate(_outcome(), _probes(passed=False))
    assert verdict is GateVerdict.DEPLOYMENT_BLOCKED


# ---------- deletion ----------

def test_tombstone_first_deletion():
    rec = DeletionRecord(resource_id="r1", state=DeletionState.REQUESTED)
    rec = advance_deletion(rec, DeletionStep.PG_TOMBSTONE)
    assert rec.state is DeletionState.TOMBSTONED
    assert query_barrier(rec)          # invisible immediately
    rec = advance_deletion(rec, DeletionStep.OUTBOX_EVENT)
    rec = advance_deletion(rec, DeletionStep.QDRANT_DELETE, ok=False)
    assert rec.state is DeletionState.PURGE_FAILED
    assert query_barrier(rec)          # still safe


def test_deletion_step_order_enforced():
    rec = DeletionRecord(resource_id="r1", state=DeletionState.REQUESTED)
    with pytest.raises(ValueError):
        advance_deletion(rec, DeletionStep.QDRANT_DELETE)


def test_full_deletion_reaches_deleted():
    rec = DeletionRecord(resource_id="r1", state=DeletionState.REQUESTED)
    for step in DeletionStep:
        rec = advance_deletion(rec, step)
    assert rec.state is DeletionState.DELETED
    assert not query_barrier(rec)


def test_cascade_invalidation():
    edges = [
        ProvenanceEdge("A", "B", "summarizes"),
        ProvenanceEdge("B", "C", "decides"),
    ]
    out = cascade_invalidation("A", edges, deleted=True)
    assert out["B"] is DerivedRelation.INVALID
    assert out["C"] is DerivedRelation.REVIEW_REQUIRED


def test_memory_deletion_scopes():
    ok, _ = validate_memory_deletion(MemoryDeletionRequest(
        scope=MemoryDeletionScope.SESSION, session_id="s1",
        memory_kind="session"))
    assert ok
    ok, reason = validate_memory_deletion(MemoryDeletionRequest(
        scope=MemoryDeletionScope.LONG_TERM))
    assert not ok  # missing memory_kind
    ok, _ = validate_memory_deletion(MemoryDeletionRequest(
        scope=MemoryDeletionScope.ALL_FOR_SUBJECT, subject_id="u1"))
    assert ok


# ---------- startup gate ----------

def test_startup_blocked_on_half_migration():
    verdict, reasons = startup_gate(StartupChecks(migration_complete=False))
    assert verdict is StartupVerdict.BLOCKED
    assert "migration-half-complete" in reasons


def test_startup_blocked_missing_generation():
    verdict, _ = startup_gate(StartupChecks(active_generation_present=False))
    assert verdict is StartupVerdict.BLOCKED


def test_startup_degraded_on_qdrant_down():
    verdict, reasons = startup_gate(StartupChecks(qdrant_ok=False))
    assert verdict is StartupVerdict.DEGRADED_READY
    assert "qdrant-unreachable" in reasons


def test_startup_canonical():
    verdict, _ = startup_gate(StartupChecks())
    assert verdict is StartupVerdict.CANONICAL_READY


# ---------- takeover ----------

def test_takeover_requires_all_12():
    results = {c: True for c in TAKEOVER_CRITERIA}
    assert evaluate_takeover(results).complete
    results["benchmark-pass"] = False
    rep = evaluate_takeover(results)
    assert not rep.complete
    assert "benchmark-pass" in rep.missing


def test_takeover_rejects_unknown_criteria():
    results = {c: True for c in TAKEOVER_CRITERIA}
    results["qdrant-reachable"] = True   # not a criterion
    assert not evaluate_takeover(results).complete


def test_post_takeover_invariant():
    # canonical healthy -> degraded never selectable post-takeover
    assert select_backend(True, True) == "canonical"
    assert select_backend(False, True) == "degraded"


# ---------- DR ----------

def test_backup_priorities():
    pg = next(b for b in BACKUP_PRIORITIES if b.component == "pg-metadata")
    qd = next(b for b in BACKUP_PRIORITIES if b.component == "qdrant")
    assert pg.must_backup and not pg.rebuildable
    assert qd.rebuildable and not qd.must_backup


def test_rebuild_from_zero():
    rep = evaluate_rebuild(REBUILD_SEQUENCE, 100, 100)
    assert rep.complete
    rep2 = evaluate_rebuild(REBUILD_SEQUENCE, 90, 100)
    assert not rep2.complete
    assert "canonical-data-in-qdrant" in rep2.gap


# ---------- upgrade plan + shadow ----------

def test_phase_gating_sequential():
    ok, _ = phase_allowed(UpgradePhase.RECOVERY, frozenset())
    assert not ok
    ok, _ = phase_allowed(
        UpgradePhase.RECOVERY,
        frozenset({UpgradePhase.CANONICAL_TAKEOVER,
                   UpgradePhase.HYBRID_CANONICALIZATION}),
    )
    assert ok


def test_shadow_query_evaluation():
    good = ShadowQueryReport(50, 0.8, 0.9, 20.0)
    ok, _ = evaluate_shadow(good)
    assert ok
    bad = ShadowQueryReport(50, 0.3, 0.5, 500.0)
    ok, fails = evaluate_shadow(bad)
    assert not ok
    assert len(fails) == 3


# ---------- migration executor (G50/P2 drift-triggered build) ----------


@pytest.mark.asyncio
async def test_apply_migration_metadata_only():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 8, 3))
    calls = []
    report = await apply_migration(
        plan, apply_metadata=lambda _plan: calls.append(_plan) or True
    )
    assert report.ok and calls
    assert report.vector_generation_built is False
    assert report.phases_completed == plan.phases


@pytest.mark.asyncio
async def test_apply_migration_blocks_missing_metadata_executor():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 8, 3))
    report = await apply_migration(plan)
    assert not report.ok
    assert report.blocked_reason == 'metadata-migration-executor-missing'
    assert MigrationPhase.APPLY_METADATA not in report.phases_completed
    assert MigrationPhase.ACTIVATE not in report.phases_completed


@pytest.mark.asyncio
async def test_apply_migration_vector_rebuild_via_governed_path():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 7, 4))
    seen = []

    async def _build(_plan):
        seen.append(_plan)
        return RebuildReport(
            steps_completed=(
                RebuildStep.START_QDRANT,
                RebuildStep.CREATE_GENERATION,
                RebuildStep.READ_PG_METADATA,
                RebuildStep.RESOLVE_SOURCES,
                RebuildStep.RECHUNK_REEMBED,
                RebuildStep.VALIDATE,
                RebuildStep.ACTIVATE,
            ),
            resources_rebuilt=10,
            resources_expected=10,
            complete=True,
        )

    report = await apply_migration(plan, build_vector_generation=_build)
    assert report.ok and report.vector_generation_built and seen
    assert report.phases_completed == plan.phases


@pytest.mark.asyncio
async def test_apply_migration_incomplete_rebuild_never_activates():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 7, 4))
    partial = RebuildReport(
        steps_completed=(
            RebuildStep.START_QDRANT,
            RebuildStep.CREATE_GENERATION,
            RebuildStep.READ_PG_METADATA,
        ),
        resources_rebuilt=0,
        resources_expected=5,
        complete=False,
        gap='stopped-mid-build',
    )
    report = await apply_migration(
        plan, build_vector_generation=lambda _plan: partial
    )
    assert not report.ok
    assert report.blocked_reason == 'vector-generation-build-incomplete'
    assert MigrationPhase.VALIDATE not in report.phases_completed
    assert MigrationPhase.ACTIVATE not in report.phases_completed


@pytest.mark.asyncio
async def test_apply_migration_missing_vector_builder_blocked():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 7, 4))
    report = await apply_migration(plan)
    assert not report.ok
    assert report.blocked_reason == 'vector-generation-builder-missing'


@pytest.mark.asyncio
async def test_apply_migration_failing_phase_stops_sequence():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 8, 4))

    def _boom(_plan):
        raise RuntimeError('pg migration blew up')

    report = await apply_migration(plan, apply_metadata=_boom)
    assert not report.ok
    assert report.blocked_reason.startswith('metadata-migration-failed')
    assert MigrationPhase.BUILD_VECTOR_GENERATION not in report.phases_completed
    assert MigrationPhase.ACTIVATE not in report.phases_completed


@pytest.mark.asyncio
async def test_apply_migration_validator_gate():
    plan = plan_migration(RagSchemaVersions(4, 7, 3), RagSchemaVersions(4, 8, 3))
    report = await apply_migration(
        plan,
        apply_metadata=lambda _plan: True,
        validate=lambda _plan: False,
    )
    assert not report.ok
    assert report.blocked_reason == 'validation-failed'
    assert MigrationPhase.ACTIVATE not in report.phases_completed


@pytest.mark.asyncio
async def test_migrate_schema_drift_triggers_vector_build():
    """G50: fingerprint-drifted generation -> migration tool -> governed
    new-generation build through rebuild_canonical."""
    from core_system.rag.pipeline_recovery import PipelineRecoveryMixin

    calls = []

    class _Pipe(PipelineRecoveryMixin):
        async def rebuild_canonical(self, mgr, **kw):
            calls.append(mgr)
            return RebuildReport(
                steps_completed=(
                    RebuildStep.CREATE_GENERATION,
                    RebuildStep.READ_PG_METADATA,
                    RebuildStep.RECHUNK_REEMBED,
                    RebuildStep.VALIDATE,
                    RebuildStep.ACTIVATE,
                ),
                resources_rebuilt=3,
                resources_expected=3,
                complete=True,
            )

    pipe = _Pipe()
    report = await pipe.migrate_schema(
        object(),
        current=RagSchemaVersions(1, 1, 1),
        target=RagSchemaVersions(1, 1, 2),
    )
    assert report.ok and report.vector_generation_built and calls
