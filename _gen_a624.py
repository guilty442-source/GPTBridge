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
    "subject": "automation-core-package-version-reconciliation",
    "rule": (
        "PACKAGE-VERSION-SYNC:the automation core owns periodic package "
        "version reconciliation as a registered periodic flow: detect "
        "installed toolchain and package versions across every governed "
        "surface (vendored toolchains, language runtimes, dependency "
        "manifests, services), run governed upgrades through the owning "
        "package manager where one exists under policy bounds, and "
        "reconcile declared codex versions to installed actuals by emitting "
        "codex-amendment-request artifacts through the governed intake "
        "pipeline -- never by direct authority writes; upgrades are "
        "fail-closed when the ecosystem is offline or vendored."
    ),
    "prohibition": (
        "FORBID:direct codex writes outside the amendment pipeline|network "
        "package fetch when a vendored toolchain exists|unbounded upgrade "
        "commands|version drift left unreconciled without an emitted "
        "amendment or recorded deferral|private scheduling outside "
        "AutomationCore"
    ),
    "exception": (
        "vendored toolchains are detect-only (their replacement is a "
        "distribution act, not a package update); retired ecosystems "
        "(Node.js, TypeScript, PyTorch, Qdrant, SQLite, Electron per A621) "
        "are never upgraded"
    ),
}

req = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "package-version-sync-mandate-20260926",
    "title": f"New article {pid}: automation core package update + codex version reconciliation",
    "summary": (
        f"Adds article {pid}: the automation core periodically detects "
        "installed package/toolchain versions, runs governed upgrades via "
        "owning package managers, and submits codex amendments so declared "
        "versions track actuals. Implements the user directive that package "
        "updates flow into codex version text automatically."
    ),
    "requested_by": "decision-sovereign",
    "origin": "user directive: automation core auto-updates packages; package versions amend the codex on update",
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
            "no governing provision authorizes or constrains automated "
            "package version reconciliation or codex version restamping"
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
                "law_code": "CODEX_AUTOMATIC_REPAIR_SPECIAL_LAW",
                "authority_basis": "A35|A488|A622|A623",
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
                "module_code": "CODEX_MODULE_MAINTENANCE",
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
       "codex-amendment-request-package-version-sync-mandate-20260926.json")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    json.dump(req, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("written:", out)
