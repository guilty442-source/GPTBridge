import json, sys, io, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    rows = c.execute(
        "SELECT ls.provision_type, ls.provision_id "
        "FROM provision_lifecycle_status ls "
        "WHERE ls.lifecycle_state='active' AND NOT EXISTS ("
        "SELECT 1 FROM provision_law_classification cl "
        "WHERE cl.provision_type=ls.provision_type "
        "AND cl.provision_id=ls.provision_id) ORDER BY 1,2"
    ).fetchall()

def basis(pid: str) -> str:
    for tag in ("CLOSURE_DELEGATED_", "RULE_DELEGATED_", "REGISTRY_"):
        if pid.startswith(tag):
            return pid[len(tag):].removesuffix("_V1")
    return "A35"

insert_rows = [
    {
        "provision_type": pt,
        "provision_id": pid,
        "tier": "main-codex",
        "law_code": "CODEX_MAIN",
        "authority_basis": basis(pid),
    }
    for pt, pid in rows
]
print("rows:", len(insert_rows))

req = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "delegated-identity-classification-20260925",
    "title": "Classify 35 active delegated normative identities under CODEX_MAIN",
    "summary": (
        "35 active provision_lifecycle_status identities (26 closure-definition "
        "CLOSURE_DELEGATED_*, 1 formal-rule RULE_DELEGATED_A498_V1, 8 "
        "registry-rule REGISTRY_A*) carry no provision_law_classification row, "
        "so the directory audit gate reports 'provision classification does not "
        "map every provision exactly once'. This request classifies all 35 "
        "under CODEX_MAIN/main-codex, matching the CODEX_MAIN classification "
        "precedent for delegated normative identities and the A610 repair. "
        "Executing this amendment also runs the rebuilt generation bookkeeping "
        "path (codex_generation_projections), which restamps the version axis "
        "and rebuilds search/FTS/manifest/revision/seal projections for the "
        "current generation."
    ),
    "requested_by": "decision-sovereign",
    "origin": (
        "post-execution audit repair: governance audit FAIL on "
        "provision_law_classification coverage (35 unclassified active "
        "delegated identities)"
    ),
    "change_class": "architecture-authority",
    "required_review": "five-sovereign-audit-unanimous-pass",
    "flow": "A382/A488-non-disruptive-amendment-flow",
    "not_executed": True,
    "auto_execute": True,
    "predecessor": {
        "codex_version": "2026-09-25T17:52:49Z",
        "version_identity": "E2:2026-09-23T03:13:43Z",
        "version_epoch": 2,
        "history_head": "bedd83c81bcc3dc3d2171bac92b6ace4b9acfa8b65e56d5ab7e6e2ac94717590",
        "revision_sequence": 75,
    },
    "problem": {
        "summary": (
            "35 active delegated identities lack provision_law_classification "
            "rows; check_provision_classification fails closed"
        )
    },
    "proposed_successors": [
        {
            "registry": "provision_law_classification",
            "action": "insert",
            "rows": insert_rows,
        }
    ],
    "verification": {
        "requested_at": datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expected_audit_result": (
            "provision classification maps every provision exactly once"
        ),
    },
}

out = (
    r"E:\GPTBridge\governance_rule\execution\audit\convergence\"
    "codex-amendment-request-delegated-identity-classification-20260925.json"
)
with open(out, "w", encoding="utf-8", newline="\n") as f:
    json.dump(req, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("written:", out)
