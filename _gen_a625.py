import json, io, sys, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    ver = c.execute(
        "SELECT value FROM metadata WHERE key='codex_version'").fetchone()[0]
    ident = c.execute(
        "SELECT value FROM metadata WHERE key='current_version_identity'"
    ).fetchone()[0]
    epoch = int(c.execute(
        "SELECT value FROM metadata WHERE key='current_version_epoch'"
    ).fetchone()[0])
    seq, head = c.execute(
        "SELECT sequence, entry_hash FROM revision_history "
        "ORDER BY sequence DESC LIMIT 1").fetchone()
    top = c.execute(
        "SELECT MAX(CAST(SUBSTRING(provision_id FROM 2) AS int)) "
        "FROM articles").fetchone()[0]

pid = f"A{top + 1}"
print("next article:", pid, "predecessor:", ver, "seq", seq)

article = {
    "position": top + 1,
    "provision_id": pid,
    "section_index": "7",
    "subject": "test-audit-final-responsibility-division",
    "rule": (
        "TEST-AUDIT-DIVISION:test and audit responsibility is assigned "
        "per language exactly: C/C++>native-suites+ABI+runtime+"
        "inference-parity; Rust>vector-engine+FFI+recovery+tauri; "
        "Go>search+batch-io+cancellation+concurrency; F#>domain-rules+"
        "state-transition+data-validation; C#>TestSuiteOrchestrator+"
        "application+contract-tests; Python>necessary-governance-tests+"
        "JAX-tests+push-gate; Julia>numerical+optimization+simulation; "
        "JavaScript>react+ipc-schema+tauri-ui-integration; "
        "PostgreSQL>migration+transaction+data-authority. "
        "EVIDENCE-CHAIN:formal evidence flows exactly native-language "
        "tests > C# TestSuiteOrchestrator > Unified Test Evidence > "
        "C++ Audit Engine > Python governance/push gate > formal verdict; "
        "no second formal test orchestrator, audit engine, or release gate "
        "may be created for Rust, Go, Julia, JAX, or any future technology."
    ),
    "prohibition": (
        "FORBID:second test orchestrator|second audit engine|second "
        "release gate|technology without declared test domain|evidence "
        "chain bypass or reorder|informal suite emitting a formal verdict"
    ),
    "exception": (
        "informal per-language diagnostic suites may exist but never "
        "produce formal verdicts; retired technologies carry no active "
        "test responsibility beyond migration evidence"
    ),
}

req = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "test-audit-final-division-20260926",
    "title": f"New article {pid}: final test/audit responsibility division + evidence chain",
    "summary": (
        f"Adds article {pid} codifying the final per-language test/audit "
        "responsibility matrix (9 surfaces) and the single formal evidence "
        "chain (native tests > C# TestSuiteOrchestrator > Unified Test "
        "Evidence > C++ Audit Engine > Python governance/push gate > "
        "formal verdict), with the standing prohibition on second "
        "orchestrators, audit engines, or release gates."
    ),
    "requested_by": "decision-sovereign",
    "origin": "user directive: formal test/audit responsibility division and unified evidence chain",
    "change_class": "clarification",
    "required_review": "five-sovereign-audit-unanimous-pass",
    "flow": "A382/A488-non-disruptive-amendment-flow",
    "not_executed": True,
    "auto_execute": True,
    "predecessor": {
        "codex_version": ver,
        "version_identity": ident,
        "version_epoch": epoch,
        "history_head": head,
        "revision_sequence": seq,
    },
    "problem": {
        "summary": (
            "A616 declares a test-responsibility requirement and A617 the "
            "tool table, but no provision pins the per-language test domain "
            "matrix or the single formal evidence chain end to end"
        )
    },
    "proposed_successors": [
        {"registry": "articles", "action": "insert", "rows": [article]},
        {
            "registry": "provision_law_classification",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": pid,
                "tier": "special-law",
                "law_code": "CODEX_TEST_VALIDATION_EVALUATION_SPECIAL_LAW",
                "authority_basis": "A210|A348|A359|A360|A616|A617",
            }],
        },
        {
            "registry": "provision_lifecycle_status",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": pid,
                "lifecycle_state": "active",
                "effective_version": "<successor-version>",
                "successor_identity": None,
                "evidence": f"{pid}-current",
                "legacy_effective_version": None,
                "current_binding_version": "<successor-version>",
            }],
        },
        {
            "registry": "codex_internal_module_membership",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": pid,
                "module_code": "CODEX_MODULE_TEST",
                "membership_kind": "primary",
                "resolution_state": "resolved",
                "version_identity": "<successor-version>",
            }],
        },
        {
            "registry": "effective_provisions",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": pid,
                "effective_version": "<successor-version>",
                "status": "active",
                "legacy_effective_version": None,
                "current_binding_version": "<successor-version>",
            }],
        },
    ],
    "verification": {
        "requested_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expected_audit_result": (
            "provision classification maps every provision exactly once; "
            "projections rebuilt for successor generation"
        ),
    },
}

out = ("E:/GPTBridge/governance_rule/execution/audit/convergence/"
       "codex-amendment-request-test-audit-final-division-20260926.json")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    json.dump(req, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("written:", out)
