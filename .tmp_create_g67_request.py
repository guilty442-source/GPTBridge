import json
from pathlib import Path

from governance_rule.execution.codex_repository import codex_readonly_connection

REQUEST_PATH = Path("main-system/runtime/state/codex-amendment-request-formal-rule-parity-lifecycle-2026-09-21-r2.json")

with codex_readonly_connection() as connection:
    codex_version = connection.execute(
        "SELECT value FROM metadata WHERE key='codex_version'"
    ).fetchone()[0]
    sequence, _change_id, version, history_head = connection.execute(
        "SELECT sequence, change_id, version, entry_hash FROM revision_history ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    rules = [
        row[0]
        for row in connection.execute(
            "SELECT rule_code FROM formal_rule_registry WHERE status='proposed' ORDER BY rule_code"
        ).fetchall()
    ]

request = {
    "artifact": "codex-amendment-request",
    "schema": "gptbridge.codex-amendment-request/v1",
    "authority": "request-only",
    "request_id": "codex-amendment-request-formal-rule-parity-lifecycle-2026-09-21-r2",
    "requested_by": "architecture-sovereign-worker",
    "change_class": "authority-duty",
    "title": "Transition proposed formal rules into evaluator-parity lifecycle",
    "summary": "Move the 42 formal rules currently recorded as proposed into declared-pending-evaluator-parity so their evaluator parity debt is explicit until the governor verifies and activates them.",
    "predecessor": {
        "codex_version": codex_version,
        "version_identity": "gptbridge-governance-codex-v" + codex_version,
        "version_epoch": 2,
        "history_head": history_head,
        "revision_sequence": sequence,
        "seal_state": "sealed-pending-external-signatures",
        "certification_state": "sealed-pending-external-signatures",
    },
    "successor_version_placeholder": "E2:<next-authoritative-utc-second>",
    "required_review": "formal-rule-parity-transition",
    "required_execution_steps": [
        "five-sovereign-audit",
        "human-governor-review",
        "governed-successor-seal",
        "external-signatures",
        "authority-re-anchor",
        "certification-complete",
    ],
    "changes": [
        {
            "table": "formal_rule_registry",
            "key": {"rule_code": rule_code},
            "field": "status",
            "proposed": "declared-pending-evaluator-parity",
            "also": {
                "version_identity": "successor",
                "parity_status": "PENDING",
            },
        }
        for rule_code in rules
    ],
    "deferred_work": [
        "Verify evaluator/test parity evidence for each declared-pending-evaluator-parity rule.",
        "Governor decides each evaluator-parity-verified to active transition only after evidence is complete.",
        "Do not mark any rule active solely because an evaluator callable exists.",
    ],
    "blockers": [
        "external-signature-completion-required-before-authority-anchor",
        "worker-does-not-seal-sign-or-publish",
    ],
    "not_executed": True,
    "do_not_execute": "worker-prepared-candidate-request-only",
    "artifact_paths": [],
}

REQUEST_PATH.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(REQUEST_PATH)
print(len(rules), sequence, codex_version, history_head)
