"""Canonical parity fact fixtures for per-operation formal-rule evaluators.

The declared-provision rules evaluate the codex itself and need no facts.
The remaining evaluators are per-operation predicates: they judge one call,
one route selection, one stage set, one payload.  Parity for those rules is
demonstrated by feeding each evaluator a canonical valid fact set and
recording the PASS outcome — the same evidence id the parity runner writes
into ``parity_evidence_id`` when the rule is promoted.

Each fixture is the *minimal* fact set that exercises the evaluator's real
pass path (not a degenerate short-circuit): e.g. the capability-preservation
fixture takes the ``remove`` branch with supersession, and the pipeline
fixture uses the declared reduced-stage path.
"""

from __future__ import annotations

from typing import Any, Mapping

_RECEIPT_PAYLOAD = {
    "receipt_id": "rcp-parity-1",
    "operation_code": "parity-check",
    "request_id": "req-parity-1",
    "actor": "parity-runner",
    "executor": "parity-runner",
    "target": "formal-rule-parity",
    "scope": "evaluator-parity",
    "generation": "g1",
    "idempotency_key": "idem-parity-1",
    "result": "PASS",
    "fault_codes": ["PARITY_FAULT"],
    "evidence_hashes": ["a" * 64],
    "started_at_utc": "2026-09-21T00:00:00Z",
    "completed_at_utc": "2026-09-21T00:00:01Z",
    "correlation_id": "corr-parity-1",
    "receipt_hash": "b" * 64,
}

PARITY_FACT_FIXTURES: dict[str, Mapping[str, Any]] = {
    # A437: flat single-context fact set — caller == callee by construction.
    "RULE_CALL_BOUNDARY": {
        "process": "parity-proc",
        "module": "parity-mod",
        "owner": "parity-owner",
        "trust": "parity-trust",
        "principal": "parity-principal",
        "generation": "parity-gen",
        "state_owner": "parity-state",
        "io": False,
        "serialization": False,
        "side_effect": False,
    },
    # A438: EXECUTOR performs execution and produces a receipt.
    "RULE_ROLE_SEPARATION": {
        "role": "EXECUTOR",
        "operation": "execution",
        "receipt": True,
    },
    # A439: tool_id -> runtime identity, generation-bound resolver receipt.
    "RULE_IDENTITY_CONVERSION": {
        "input_namespace": "tool_id",
        "input_id": "parity-tool",
        "generation": "g1",
        "registry_generation": "g1",
        "registry_hash": "c" * 64,
        "resolver_id": "parity-resolver",
        "resolver_receipt": {
            "resolver_id": "parity-resolver",
            "registry_generation": "g1",
            "status": "ok",
        },
        "output_ids": ["parity-runtime"],
    },
    # A440: exactly one canonical suite.
    "RULE_TEST_SUITE_CARDINALITY": {
        "module_code": "parity-module",
        "suite_ids": ["parity-suite"],
    },
    # A442: typed contract + typed signals.
    "RULE_HEALTH_CONTRACT": {
        "contract": {
            "health_contract_id": "hc-parity",
            "module_code": "parity-module",
            "dimensions": ["availability"],
        },
        "schema_version": "health-contract/v1",
        "generation": "g1",
        "signals": [
            {
                "signal_id": "sig-1",
                "dimension": "availability",
                "value": 1,
                "unit": "ratio",
                "observed_at_utc": "2026-09-21T00:00:00Z",
            }
        ],
    },
    # A445: scope resolves through an explicit successor.
    "RULE_PRECEDENCE_RESOLUTION": {
        "scope": "parity-scope",
        "successor_graph": {"parity-scope": "controlling-rule"},
    },
    # A446: declared reduced-stage owner-internal path.
    "RULE_EXECUTION_PIPELINE": {
        "stages": ["INTAKE", "EXECUTE", "RESULT"],
    },
    # A447: LOCAL_PURE_CALL with all contexts equal, no io/mutation.
    "RULE_CONNECTION_ROUTE": {
        "selected_route": "LOCAL_PURE_CALL",
        "process": "x",
        "owner": "x",
        "trust": "x",
        "principal": "x",
        "state_owner": "x",
        "io": False,
        "mutation": False,
    },
    # A448: RECEIPT_V1 payload with every required field.
    "RULE_MACHINE_SCHEMA": {
        "schema_code": "RECEIPT_V1",
        "version": "1",
        "payload": _RECEIPT_PAYLOAD,
    },
    # A449: technical pass + governed certification closure.
    "RULE_RELEASE_VERIFICATION": {
        "technical_status": "TECHNICAL_PASS",
        "authorization_status": "VALID",
        "integrity_status": "VALID",
        "lineage_status": "VALID",
        "trust_anchor": "VALID",
    },
    # A77: convergence with parity evidence and a resolved classification.
    "RULE_CODEX_CONVERGENCE_V1": {
        "parity_evidence": "parity-run-evidence",
        "classification": "PASS",
    },
    # A498: full delegated contract fact set.
    "RULE_DELEGATED_A498_V1": {
        "machine_schema_registry": {"RECEIPT_V1": "registered"},
        "chinese_mirror_parts": {"part-1": "rendered"},
        "codex_read_session": {"single_use": True},
        "identity_conversion": {"contract": "A439"},
        "identity_resolution_evidence": {"receipt": "bound"},
        "formal_rule_evidence_schemas": [f"SCHEMA_{i}" for i in range(10)],
        "gate_evidence": {"gate": "recorded"},
        "audit_event": {"redaction_class": "public"},
        "json_text_usage": "descriptor-only",
    },
    # A446: independent verifier distinct from executor, with evidence.
    "RULE_EXECUTION_VERIFICATION_SEPARATION_V1": {
        "executor": "parity-executor",
        "verifier": "parity-verifier",
        "verification_evidence": {"evidence_id": "ev-parity"},
        "requires_independent_verification": True,
    },
    # A446: state-changing file op with all six evidence classes.
    "RULE_FILE_OPERATION_RELIABILITY_V1": {
        "operation": "write",
        "lock_evidence": "lock-1",
        "staging_evidence": "staging-1",
        "integrity_evidence": "integrity-1",
        "journal_evidence": "journal-1",
        "recovery_evidence": "recovery-1",
        "final_receipt": "receipt-1",
    },
    # A77: removal under explicit supersession with all paths preserved.
    "RULE_MATURE_CAPABILITY_PRESERVATION_V1": {
        "capability_id": "parity-capability",
        "action": "remove",
        "explicit_supersession": "superseding-capability",
        "contract_preserved": True,
        "test_preserved": True,
        "evidence_path_preserved": True,
    },
    # Registry-owned convergence domains: evidence present, no violations.
    "RULE_GIT_WORKTREE_V1": {"evidence": {"worktree": "registered"}},
    "RULE_RAG_PROVENANCE_V1": {"evidence": {"provenance": "recorded"}},
    "RULE_NATIVE_PROMOTION_V1": {"evidence": {"promotion": "recorded"}},
    "RULE_BACKEND_HANDOFF_V1": {"evidence": {"handoff": "recorded"}},
    "RULE_MODEL_DIALOGUE_MODE_V1": {"evidence": {"mode": "recorded"}},
    "RULE_TOOL_RUNTIME_V1": {"evidence": {"runtime": "recorded"}},
    # A334: registered module, one assignment, exact authority separation.
    "RULE_RUNTIME_AUTHORITY_RESOLUTION_V1": {
        "module_code": "parity-module",
        "module_assignment": "assignment-1",
        "parent_sovereign": "sovereign-1",
        "separated_authorities": {
            "decision": "sov-decision",
            "permission": "sov-permission",
            "execution": "sov-execution",
            "runtime": "sov-runtime",
        },
        "executor": "parity-executor",
        "generation": "g1",
        "authority_overlap": False,
    },
    # A516: all roots present, hashes equal, zero findings, tests pass.
    "RULE_SQL_GOVERNANCE_CLOSURE": {
        "governance_roots": {
            "postgres_catalog_hash": "h",
            "migration_chain_hash": "h",
            "live_schema_hash": "h",
            "security_projection_hash": "h",
            "sqlite_scope_hash": "h",
            "reconciliation_hash": "h",
            "audit_contract_hash": "h",
            "transport_contract_hash": "h",
            "test_evidence_hash": "h",
        },
        "declared_schema_hash": "h",
        "replayed_schema_hash": "h",
        "live_introspected_schema_hash": "h",
        "open_findings": 0,
        "tests_pass": True,
    },
    # A590: five-core budget — 2 threads × 2 workers = 4 ≤ 5, bounded.
    "FR-MULTI-CORE-PARALLEL": {
        "budget_cores": 5,
        "threads_per_worker": 2,
        "parallel_workers": 2,
        "unbounded_pools": 0,
    },
    # A502: current schema hash equals migrations result hash, parity exact.
    "RULE_SQL_MIGRATION_AUTHORITY": {
        "current_postgresql_schema_hash": "h",
        "ordered_verified_migrations_result_hash": "h",
        "directory_catalog_parity": True,
        "catalog_parity": True,
    },
    # A604/A592: declared request resolves to exactly one eligible active
    # module under a valid lease with the dispatch decision recorded.
    "RULE_CAPABILITY_DISPATCH_V1": {
        "requirements": {
            "capability_codes": ["cap-parity"],
            "permission_scope": ["perm-parity"],
            "resource_constraints": {"cpu": 4},
            "contract_version": "v1",
        },
        "candidates": [
            {
                "module_identity": "parity-module",
                "single_responsibility": "parity dispatch target",
                "capability_codes": ["cap-parity"],
                "resource_requirements": {"cpu": 1},
                "permission_requirements": ["perm-parity"],
                "contract_version": "v1",
                "runtime_state": "active",
                "availability": "ready",
                "version": "1",
                "owner_engine_domain": "parity-domain",
                "execution_identity": "parity-executor",
            },
        ],
        "lease": {"valid": True, "fencing_token": "ftok-parity-1"},
        "decision": {"recorded": True},
    },
}


def parity_fact_fixtures() -> Mapping[str, Mapping[str, Any]]:
    """Return the canonical parity fact fixtures (copy per call)."""
    return {code: dict(facts) for code, facts in PARITY_FACT_FIXTURES.items()}
