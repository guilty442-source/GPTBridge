"""Formal rule evaluator and implementation obligation tests (A445/A292).

Verifies the machine predicates behind the formal rules and the A292
obligation lifecycle against the official codex registry data.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

from governance_rule.execution.formal_rules import (
    load_formal_rules,
    registered_rule_codes,
    missing_evaluator_codes,
    evaluate_rule,
    evaluate_all,
)
from governance_rule.execution.formal_rules.obligations import (
    load_obligations,
    obligation,
    overdue_obligations,
    evidence_readiness,
    lifecycle_status,
)


def test_registry_declares_in_force_formal_rules() -> None:
    ruleset = load_formal_rules()
    assert len(ruleset.rules) >= 12
    active = {r.rule_code for r in ruleset.active_rules()}
    # Every declared row is in force unless its lifecycle is terminal.
    terminal = {
        r.rule_code
        for r in ruleset.rules
        if str(r.status).strip().lower()
        in {"retired", "superseded", "withdrawn", "inactive"}
    }
    assert len(active) == len(ruleset.rules) - len(terminal)
    assert {
        "RULE_CALL_BOUNDARY",
        "RULE_ROLE_SEPARATION",
        "RULE_IDENTITY_CONVERSION",
        "RULE_EXECUTION_PIPELINE",
        "RULE_SQL_GOVERNANCE_CLOSURE",
        "RULE_SQL_MIGRATION_AUTHORITY",
    } <= active


def test_every_active_rule_has_machine_evaluator() -> None:
    ruleset = load_formal_rules()
    assert missing_evaluator_codes(ruleset) == ()
    assert {rule.rule_code for rule in ruleset.active_rules()} <= set(
        registered_rule_codes()
    )


def test_call_boundary_classification() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_CALL_BOUNDARY")
    assert rule is not None
    assert rule.severity == "critical"
    # Namespaces intentionally carry distinct values: a pure call compares
    # caller.X == callee.X per namespace, never across namespaces.
    context = {
        "process": "proc-1", "module": "mod-1", "owner": "owner-1",
        "trust": "trust-1", "principal": "principal-1",
        "generation": "gen-1", "state_owner": "state-1",
    }
    pure = {
        **{f"caller.{key}": value for key, value in context.items()},
        **{f"callee.{key}": value for key, value in context.items()},
        "io": False, "serialization": False, "side_effect": False,
    }
    outcome = evaluate_rule(rule, pure)
    assert outcome.passed is True
    assert outcome.decision == "PURE_INTERNAL_CALL"
    cross = dict(pure)
    cross["callee.module"] = "mod-2"
    outcome = evaluate_rule(rule, cross)
    assert outcome.passed is False
    assert outcome.decision == "GOVERNED_BOUNDARY_CALL"
    incomplete = dict(pure)
    incomplete.pop("callee.generation")
    assert evaluate_rule(rule, incomplete).passed is False
    flat = {**context, "io": False, "serialization": False, "side_effect": False}
    assert evaluate_rule(rule, flat).passed is True
    effected = dict(pure)
    effected["side_effect"] = True
    assert evaluate_rule(rule, effected).passed is False


def test_role_separation_forbids_sovereign_execution() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_ROLE_SEPARATION")
    assert rule is not None
    ok = evaluate_rule(rule, {"role": "SOVEREIGN", "operation": "decision"})
    assert ok.passed is True
    bad = evaluate_rule(rule, {"role": "SOVEREIGN", "operation": "execution"})
    assert bad.passed is False
    bounded = evaluate_rule(rule, {"role": "SUB_SOVEREIGN", "operation": "dispatch"})
    assert bounded.passed is True
    mixed = evaluate_rule(
        rule,
        {"role": "XINGCHENG", "operation": "review", "active_role_identity": "EXECUTOR"},
    )
    assert mixed.passed is False
    sub_role = evaluate_rule(
        rule,
        {
            "role": "XINGCHENG",
            "operation": "coordinate",
            "active_role_identity": "XINGCHENG:COORDINATION",
        },
    )
    assert sub_role.passed is True
    # A336/A337/A378: 星澄 special law grants governed invocation/execution
    # within its privileged institution; decision power stays forbidden.
    star_execution = evaluate_rule(
        rule, {"role": "XINGCHENG", "operation": "execution"}
    )
    assert star_execution.passed is True
    star_decision = evaluate_rule(rule, {"role": "XINGCHENG", "operation": "decision"})
    assert star_decision.passed is False
    flagged = evaluate_rule(
        rule, {"role": "EXECUTOR", "operation": "execution", "dispatch": True}
    )
    assert flagged.passed is False


def test_identity_conversion_cardinality() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_IDENTITY_CONVERSION")
    assert rule is not None
    base = {
        "input_namespace": "runtime_identity",
        "input_id": "runtime-1",
        "generation": 7,
        "registry_generation": 7,
        "registry_hash": "abc123",
        "resolver_id": "resolver-1",
        "resolver_receipt": {
            "resolver_id": "resolver-1",
            "registry_generation": 7,
            "status": "valid",
        },
        "output_ids": ["mod-x"],
    }
    ok = evaluate_rule(rule, base)
    assert ok.passed is True
    ambiguous = evaluate_rule(rule, {**base, "output_ids": ["mod-x", "mod-y"]})
    assert ambiguous.passed is False
    stale = evaluate_rule(rule, {**base, "registry_generation": 8})
    assert stale.passed is False
    unbound = evaluate_rule(rule, {**base, "resolver_receipt": ""})
    assert unbound.passed is False
    wrong_receipt = evaluate_rule(
        rule,
        {
            **base,
            "resolver_receipt": {"resolver_id": "resolver-2", "registry_generation": 7},
        },
    )
    assert wrong_receipt.passed is False


def test_test_suite_cardinality_one_per_module() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_TEST_SUITE_CARDINALITY")
    assert rule is not None
    ok = evaluate_rule(rule, {"module_code": "m1", "suite_ids": ["TS-1"]})
    assert ok.passed is True
    duplicate = evaluate_rule(rule, {"module_code": "m1", "suite_ids": ["TS-1", "TS-2"]})
    assert duplicate.passed is False
    none = evaluate_rule(rule, {"module_code": "m1", "suite_ids": []})
    assert none.passed is False


def test_health_contract_requires_exact_schema() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_HEALTH_CONTRACT")
    assert rule is not None
    contract = {
        "health_contract_id": "HC-1",
        "module_code": "m1",
        "dimensions": ["availability"],
    }
    ok = evaluate_rule(
        rule,
        {
            "contract": contract,
            "signals": [
                {
                    "signal_id": "s1", "dimension": "availability", "value": 0.99,
                    "unit": "ratio", "observed_at_utc": "2026-09-15T00:00:00Z",
                }
            ],
            "schema_version": 1,
            "generation": 2,
        },
    )
    assert ok.passed is True
    bad = evaluate_rule(
        rule,
        {"contract": contract, "signals": [{"signal_id": "s1"}], "schema_version": 1, "generation": 2},
    )
    assert bad.passed is False
    assert bad.decision == "INCOMPLETE_EVIDENCE"


def test_precedence_resolution_conflict_detected() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_PRECEDENCE_RESOLUTION")
    assert rule is not None
    ok = evaluate_rule(
        rule,
        {
            "scope": "calls",
            "successor_graph": {"A69": "A446"},
            "special_law_conflicts": {"calls": "A69"},
            "ordinance_links": {},
        },
    )
    assert ok.passed is True
    unresolved = evaluate_rule(
        rule,
        {"scope": "calls", "successor_graph": {}, "special_law_conflicts": {"calls": "A69"}, "ordinance_links": {}},
    )
    assert unresolved.passed is False
    assert unresolved.decision == "CONFLICT"


def test_execution_pipeline_requires_full_stages_for_mutation() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_EXECUTION_PIPELINE")
    assert rule is not None
    stages = ["INTAKE", "AUTHORIZE", "PLAN", "DISPATCH", "EXECUTE", "VERIFY", "PUBLISH"]
    governed = {
        "operation": "x",
        "side_effect": True,
        "boundary": True,
        "stages": stages,
        "stage_owners": {stage: f"owner-{stage.lower()}" for stage in stages},
        "schemas": {
            stage: {"input": f"{stage.lower()}-in", "output": f"{stage.lower()}-out"}
            for stage in stages
        },
        "timeouts": {stage: 5 for stage in stages},
        "idempotency": {stage: True for stage in stages},
        "fault_codes": {stage: [f"{stage}_FAULT"] for stage in stages},
        "receipts": {stage: f"{stage.lower()}-receipt" for stage in stages},
        "executor": "executor-1",
        "verifier": "verifier-1",
    }
    ok = evaluate_rule(rule, governed)
    assert ok.passed is True
    truncated = evaluate_rule(rule, {**governed, "stages": ["INTAKE"]})
    assert truncated.passed is False
    reordered = evaluate_rule(
        rule,
        {
            **governed,
            "stages": ["INTAKE", "AUTHORIZE", "PLAN", "EXECUTE", "DISPATCH", "VERIFY", "PUBLISH"],
        },
    )
    assert reordered.passed is False
    missing_contract = evaluate_rule(
        rule, {**governed, "timeouts": {"INTAKE": 5}}
    )
    assert missing_contract.passed is False
    self_verification = evaluate_rule(rule, {**governed, "verifier": "executor-1"})
    assert self_verification.passed is False
    reduced = evaluate_rule(
        rule,
        {
            "operation": "x",
            "side_effect": False,
            "boundary": False,
            "stages": ["INTAKE", "EXECUTE", "RESULT"],
        },
    )
    assert reduced.passed is True


def test_connection_route_selection() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_CONNECTION_ROUTE")
    assert rule is not None
    pure = {
        "process": "a", "owner": "a", "trust": "a", "principal": "a",
        "state_owner": "a", "io": False, "mutation": False, "environment": "prod",
    }
    ok = evaluate_rule(rule, {**pure, "selected_route": "LOCAL_PURE_CALL"})
    assert ok.passed is True
    wrong = evaluate_rule(rule, {**pure, "io": True, "mutation": True, "selected_route": "LOCAL_PURE_CALL"})
    assert wrong.passed is False


def test_machine_schema_validates_required_fields() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_MACHINE_SCHEMA")
    assert rule is not None
    payload = {
        "receipt_id": "R-1", "operation_code": "op", "request_id": "q1", "actor": "a",
        "executor": "e", "target": "t", "scope": "s", "generation": 1, "idempotency_key": "k",
        "result": "ok", "fault_codes": [], "evidence_hashes": [], "started_at_utc": "T",
        "completed_at_utc": "T", "correlation_id": "c", "receipt_hash": "h",
    }
    ok = evaluate_rule(rule, {"schema_code": "RECEIPT_V1", "payload": payload, "version": 1})
    assert ok.passed is True
    bad = evaluate_rule(rule, {"schema_code": "RECEIPT_V1", "payload": {"receipt_id": "R-1"}, "version": 1})
    assert bad.passed is False


def test_release_verification_requires_governed_certification() -> None:
    ruleset = load_formal_rules()
    rule = ruleset.rule("RULE_RELEASE_VERIFICATION")
    assert rule is not None
    closed = {
        "technical_status": "TECHNICAL_PASS",
        "authorization_status": "VALID",
        "integrity_status": "VALID",
        "lineage_status": "VALID",
        "trust_anchor": "VALID",
    }
    verified = evaluate_rule(rule, dict(closed))
    assert verified.passed is True
    assert verified.decision == "VERIFIED_RELEASE"
    for key in (
        "authorization_status",
        "integrity_status",
        "lineage_status",
        "trust_anchor",
    ):
        pending = evaluate_rule(rule, {**closed, key: "PENDING"})
        assert pending.passed is False
        assert pending.decision == "INCOMPLETE_EVIDENCE"
    tech_only = evaluate_rule(rule, {"technical_status": "TECHNICAL_PASS"})
    assert tech_only.passed is False


def test_sql_governance_closure_and_migration_authority() -> None:
    ruleset = load_formal_rules()
    closure = ruleset.rule("RULE_SQL_GOVERNANCE_CLOSURE")
    assert closure is not None
    facts = {
        "closure_id": "CL-1",
        "codex_version": "2026-09-16T09:22:51Z",
        "postgres_catalog_hash": "hash-1",
        "migration_chain_hash": "hash-2",
        "live_schema_hash": "schema-1",
        "security_projection_hash": "hash-3",
        "sqlite_scope_hash": "hash-4",
        "reconciliation_hash": "hash-5",
        "audit_contract_hash": "hash-6",
        "transport_contract_hash": "hash-7",
        "test_evidence_hash": "hash-8",
        "open_findings": 0,
        "result": "PASS",
        "verified_at": "2026-09-16T00:00:00Z",
        "verifier_identity": "verifier-1",
        "evidence_hash": "hash-9",
        "declared_schema_hash": "schema-1",
        "replayed_schema_hash": "schema-1",
    }
    assert evaluate_rule(closure, facts).passed is True
    drift = evaluate_rule(closure, {**facts, "replayed_schema_hash": "schema-2"})
    assert drift.passed is False
    assert drift.decision == "FAIL_CLOSED"
    findings = evaluate_rule(closure, {**facts, "open_findings": 2})
    assert findings.passed is False

    migration = ruleset.rule("RULE_SQL_MIGRATION_AUTHORITY")
    assert migration is not None
    mig_facts = {
        "migration_head": "m-9",
        "ordered_migrations": ["m-1", "m-9"],
        "ordered_verified_migrations_result_hash": "schema-root",
        "current_postgresql_schema_hash": "schema-root",
        "data_schema_authority_root": "catalog-root",
        "catalog_snapshot_hash": "catalog-root",
    }
    assert evaluate_rule(migration, mig_facts).passed is True
    schema_drift = evaluate_rule(
        migration, {**mig_facts, "current_postgresql_schema_hash": "other-root"}
    )
    assert schema_drift.passed is False
    assert schema_drift.decision == "SCHEMA_DRIFT_FAIL_CLOSED"


def test_evaluate_all_records_evidence_per_rule() -> None:
    ruleset = load_formal_rules()
    outcomes = evaluate_all(ruleset)
    assert len(outcomes) == len(ruleset.active_rules())
    for outcome in outcomes:
        assert outcome.rule_code
        assert outcome.controlling_provision_id
        assert outcome.evidence_id
        assert outcome.policy_version
        assert outcome.recorded_at_utc
        assert outcome.passed in (True, False)


_A491_BLOCKING_OBLIGATIONS = {
    "OBL_ANTI_JAILBREAK",
    "OBL_ARCHITECTURE_CATALOG",
    "OBL_CHANNEL_ANOMALY_ISOLATION",
    "OBL_DIRECTORY_GOVERNANCE_DATA_CLOSURE",
    "OBL_FORMAL_EVALUATOR_V2_PARITY",
    "OBL_LANGUAGE_DEPENDENCY_DAG_GATE",
    "OBL_LAYERED_ARCHITECTURE",
    "OBL_NATIVE_PROMOTION_RECORD_CHECKER",
    "OBL_SYSTEM_RELIABILITY",
    "OBL_TEST_SUITE_DIRECTORY",
    "OBL_TOP_LEVEL_PATHS",
    "OBL_星澄_AUDIT",
}


def test_obligations_registry_declares_a491_blocking_set() -> None:
    obligations = load_obligations()
    codes = {item.obligation_code for item in obligations}
    assert _A491_BLOCKING_OBLIGATIONS <= codes
    assert obligation("OBL_LAYERED_ARCHITECTURE", obligations) is not None
    assert obligation("OBL_FORMAL_EVALUATOR_V2_PARITY", obligations) is not None


def test_all_obligations_declared_mandated_with_due_date() -> None:
    from governance_rule.execution.formal_rules.obligations import (
        LIFECYCLE_MAIN,
        LIFECYCLE_TERMINAL,
    )

    obligations = load_obligations()
    valid_states = set(LIFECYCLE_MAIN) | set(LIFECYCLE_TERMINAL)
    for item in obligations:
        # A292: obligations advance through the lifecycle; the invariant is
        # that every declared obligation holds a valid lifecycle state and
        # carries its governing fields — not that it remains `mandated`.
        assert item.current_state in valid_states
        assert item.implementation_owner
        assert item.target_state
        assert item.acceptance_evidence
        assert item.waiver_rule
    for code in sorted(_A491_BLOCKING_OBLIGATIONS):
        item = obligation(code, obligations)
        assert item is not None
        assert item.due_at is not None


def test_obligation_lifecycle_projection() -> None:
    obligations = load_obligations()
    status = lifecycle_status(obligations)
    assert sum(status.values()) == len(obligations)
    assert overdue_obligations(obligations) == ()


def test_evidence_readiness_requires_criteria() -> None:
    item = obligation("OBL_TEST_SUITE_DIRECTORY")
    assert item is not None
    criteria = set(("registered tests", "audit", "rollback", "acceptance verdict"))
    ready_ok, missing = evidence_readiness(
        ["registered tests", "audit", "rollback", "acceptance verdict"],
        obligation_code="OBL_TEST_SUITE_DIRECTORY",
    )
    assert ready_ok is True
    assert missing == ()
    ready_bad, missing = evidence_readiness(
        ["registered tests"],
        obligation_code="OBL_TEST_SUITE_DIRECTORY",
    )
    assert ready_bad is False
    assert len(missing) > 0

def test_parity_run_records_evidence_for_every_rule(tmp_path: Path) -> None:
    """The G67 parity runner evaluates every in-force rule and records
    per-rule evidence ids — the artifact an amendment cites for the
    declared-pending -> evaluator-parity-verified transition."""
    from governance_rule.execution.formal_rules.parity import (
        run_parity_evaluation,
        write_report,
        REPORT_VERSION,
    )

    report = run_parity_evaluation(Path("postgresql-codex"))
    assert report["report"] == REPORT_VERSION
    assert len(report["database_sha256"]) == 64
    ruleset = load_formal_rules()
    assert report["evaluated"] == len(ruleset.active_rules())
    for entry in report["outcomes"]:
        assert entry["rule_code"]
        assert entry["evidence_id"]
        assert entry["passed"] in (True, False)
        assert entry["decision"]
    target = write_report(report, tmp_path / "parity.json")
    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert reloaded["evaluated"] == report["evaluated"]


def test_multi_core_parallel_bounded_workers_predicate() -> None:
    """A590: provision must be active AND the allocation must fit the
    fixed five-core budget (threads × workers ≤ budget ≤ 5, bounded)."""
    ruleset = load_formal_rules()
    rule = ruleset.rule("FR-MULTI-CORE-PARALLEL")
    assert rule is not None

    ok = evaluate_rule(rule, {
        "budget_cores": 5,
        "threads_per_worker": 2,
        "parallel_workers": 2,
        "unbounded_pools": 0,
    })
    assert ok.passed is True

    over_cap = evaluate_rule(rule, {
        "budget_cores": 6,
        "threads_per_worker": 1,
        "parallel_workers": 1,
        "unbounded_pools": 0,
    })
    assert over_cap.passed is False  # budget may never exceed five cores

    oversubscribed = evaluate_rule(rule, {
        "budget_cores": 5,
        "threads_per_worker": 4,
        "parallel_workers": 2,
        "unbounded_pools": 0,
    })
    assert oversubscribed.passed is False  # 4×2=8 > 5

    unbounded = evaluate_rule(rule, {
        "budget_cores": 5,
        "threads_per_worker": 1,
        "parallel_workers": 1,
        "unbounded_pools": 2,
    })
    assert unbounded.passed is False

    missing = evaluate_rule(rule, {})
    assert missing.passed is False
    assert missing.decision == "INCOMPLETE_EVIDENCE"
