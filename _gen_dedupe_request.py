import json
from pathlib import Path

EVIDENCE = {
    "RULE_JAX_PYTORCH_V1": "c0b542ded7a904db9a50",
    "RULE_LANGUAGE_TABLE_OFFICIAL_V1": "32df9b63da0783d62291",
    "RULE_TEST_AUDIT_RESPONSIBILITY_V1": "24039a7b17c4fcd429aa",
    "RULE_RETIRED_DEPENDENCIES_V1": "7b343ad3d187945ccb3b",
    "RULE_VECTOR_ENGINE_RUST_V1": "e3d55dde715e87fc2745",
    "RULE_JS_NATIVE_V1": "4e026d01da703a8767ac",
    "RULE_GO_RUST_NODE_V1": "2f5579cb87ec5ad3348f",
    "RULE_TEST_TOOLS_V1": "538102bd464efdbf6c09",
    "RULE_RUST_TESTS_V1": "85ce5d77dbb8d6c2de4a",
    "RULE_JAX_PARITY_V1": "8f50be5528567a3c7476",
    "RULE_FINAL_ARCHITECTURE_V1": "2668a3b0f1b44a7e550d",
}

# keeper version_identity for the three duplicated codes
KEEPER_VER = "2026-09-25T14:24:47Z"
DUP_VER = "2026-09-25T14:31:38Z"
DUPED = ("RULE_VECTOR_ENGINE_RUST_V1", "RULE_JS_NATIVE_V1", "RULE_GO_RUST_NODE_V1")

SCOPE = {
    "RULE_JAX_PYTORCH_V1": "ml-framework",
    "RULE_LANGUAGE_TABLE_OFFICIAL_V1": "language-table",
    "RULE_TEST_AUDIT_RESPONSIBILITY_V1": "test-audit",
    "RULE_RETIRED_DEPENDENCIES_V1": "retired-deps",
    "RULE_VECTOR_ENGINE_RUST_V1": "vector-engine",
    "RULE_JS_NATIVE_V1": "frontend-language",
    "RULE_GO_RUST_NODE_V1": "runtime-migration",
    "RULE_TEST_TOOLS_V1": "test-tools",
    "RULE_RUST_TESTS_V1": "rust-tests",
    "RULE_JAX_PARITY_V1": "jax-parity",
    "RULE_FINAL_ARCHITECTURE_V1": "final-architecture",
}

ops = []
# 1) withdraw the three duplicate (later) rows
for code in DUPED:
    ops.append({
        "registry": "formal_rule_registry",
        "action": "update",
        "key": {"rule_code": code, "version_identity": DUP_VER},
        "set": {"status": "withdrawn"},
    })

# 2) parity transitions for all eleven pending rules
order = list(DUPED) + [
    "RULE_JAX_PYTORCH_V1",
    "RULE_LANGUAGE_TABLE_OFFICIAL_V1",
    "RULE_TEST_AUDIT_RESPONSIBILITY_V1",
    "RULE_RETIRED_DEPENDENCIES_V1",
    "RULE_TEST_TOOLS_V1",
    "RULE_RUST_TESTS_V1",
    "RULE_JAX_PARITY_V1",
    "RULE_FINAL_ARCHITECTURE_V1",
]
for code in order:
    key = {"rule_code": code}
    if code in DUPED:
        key["version_identity"] = KEEPER_VER
    ops.append({
        "registry": "formal_rule_registry",
        "action": "update",
        "key": dict(key),
        "set": {"status": "declared-pending-evaluator-parity"},
    })
    ops.append({
        "registry": "formal_rule_registry",
        "action": "update",
        "key": dict(key),
        "set": {
            "status": "evaluator-parity-verified",
            "parity_status": "VERIFIED",
            "parity_evidence_id": EVIDENCE[code],
            "version_identity": "<successor-version>",
        },
    })

# 3) ownership-map inserts
ops.append({
    "registry": "formal_rule_ownership_map",
    "action": "insert",
    "rows": [
        {
            "invariant_code": f"{SCOPE[c]}:{c}",
            "rule_code": c,
            "owner_domain": SCOPE[c],
            "status": "current",
            "version_identity": "<successor-version>",
        }
        for c in order
    ],
})

req = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "formal-rule-dedupe-parity-activation-20260925",
    "title": "Withdraw three duplicate formal-rule registry rows and advance all eleven pending rules to evaluator-parity-verified with ownership-map rows",
    "summary": (
        "formal-rule-batch-registration-20260925-r3 re-inserted RULE_VECTOR_ENGINE_RUST_V1, "
        "RULE_JS_NATIVE_V1 and RULE_GO_RUST_NODE_V1 which "
        "final-language-and-package-division-20260925 had already inserted at version_identity "
        "2026-09-25T14:24:47Z. This request withdraws only the three duplicate rows at "
        "version_identity 2026-09-25T14:31:38Z (composite key rule_code+version_identity; "
        "proposed->withdrawn is a legal terminal transition) and then promotes all eleven "
        "proposed rules through declared-pending-evaluator-parity to evaluator-parity-verified "
        "with recorded parity evidence, plus eleven ownership-map current rows."
    ),
    "requested_by": "decision-sovereign",
    "origin": (
        "human-governor directive 2026-09-25: dedupe cleanup keeps the earlier 14:24:47Z rows "
        "as authoritative and withdraws only the later 14:31:38Z duplicates; parity activation "
        "follows the same two-step transition used by cfamily/versions activations; parity run "
        "formal-rule-parity-batch-20260925.json 85/85 PASS"
    ),
    "change_class": "clarification",
    "required_review": "five-sovereign-audit-unanimous-pass",
    "flow": "A382/A488-non-disruptive-amendment-flow",
    "not_executed": True,
    "predecessor": {
        "codex_version": "2026-09-25T14:31:38Z",
        "version_identity": "E2:2026-09-23T03:13:43Z",
        "version_epoch": 2,
        "history_head": "bedd83c81bcc3dc3d2171bac92b6ace4b9acfa8b65e56d5ab7e6e2ac94717590",
        "revision_sequence": 75,
    },
    "proposed_successors": ops,
    "mapping_basis": {
        "source": "governance_rule/execution/formal_rules/parity.py run + formal_rules/fact_fixtures.py canonical fixtures",
        "rule": "RULE_STATE_TRANSITIONS chain enforced by codex_amendment_contract.validate_rule_transition; proposed->withdrawn is a legal terminal transition used for dedupe",
        "ownership_row": "formal_rule_ownership_map convention <owner_domain>:<rule_code> with status=current",
    },
    "required_execution_steps": [
        "five-sovereign-audit",
        "human-governor-review",
        "governed-successor-seal",
        "authority-re-anchor",
        "certification-complete",
    ],
    "out_of_scope": [
        "deletion of duplicate rows (successor builder supports insert/update only; withdrawn is the terminal non-active state)",
        "semantic_hash/evaluator_hash/test_contract_hash/controlling_provision_hash on the rule rows (governor-side seal-time data)",
        "actual toolchain or runtime migrations (governance records only)",
    ],
    "evidence": [
        "parity run main-system/runtime/state/formal-rule-parity-batch-20260925.json: 85/85 PASS covering all eleven pending rules",
        "duplicate detection: formal_rule_registry holds two proposed rows each for RULE_VECTOR_ENGINE_RUST_V1, RULE_JS_NATIVE_V1, RULE_GO_RUST_NODE_V1; keeper rows carry version_identity 2026-09-25T14:24:47Z, duplicates carry 2026-09-25T14:31:38Z",
        "evaluators registered in governance_rule/execution/formal_rules/evaluators.py for all eleven rule codes; running backend restarted after evaluators.py update so the in-process registry is current",
    ],
}

out = Path(r"E:\GPTBridge\main-system\runtime\state\codex-amendment-request-formal-rule-dedupe-parity-20260925.json")
out.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
print("written:", out, "ops:", len(ops))
