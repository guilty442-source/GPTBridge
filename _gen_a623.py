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
    "subject": "shared-dotnet10-runtime-csharp-fsharp",
    "rule": (
        "SHARED-DOTNET10:C# and F# execute on exactly one shared .NET 10 "
        "runtime -- one CLR, one GC, one unified base class library and one "
        "runtime version; F# domain business-logic and C# application/"
        "workflow/API/test-orchestration surfaces are separated by "
        "responsibility, never by runtime; sharing the runtime is the "
        "canonical fulfillment of A622 shareable-implementation reuse and "
        "A35/A610/A615 already-declared .NET 10 sharing."
    ),
    "prohibition": (
        "FORBID:second CLR or .NET runtime version|F# on a runtime other "
        "than the shared .NET 10|duplicated per-language BCL/toolchain where "
        "the shared .NET 10 toolchain suffices|runtime-boundary used to "
        "smuggle responsibility drift between C# orchestration and F# domain"
    ),
    "exception": (
        "native C ABI interop boundaries remain governed by A217/A341; "
        "non-.NET languages keep their own runtimes"
    ),
}

req = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "shared-dotnet10-runtime-20260926",
    "title": f"New article {pid}: single shared .NET 10 runtime for C# and F#",
    "summary": (
        f"Adds article {pid} codifying that C# and F# share exactly one "
        ".NET 10 runtime (one CLR/GC/BCL/runtime version). Restates as a "
        "controlling provision what A35/A610/A615 declare inline, under "
        "A622's shareable-implementation mandate."
    ),
    "requested_by": "decision-sovereign",
    "origin": "user directive: F# and C# share .NET 10",
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
            "shared .NET 10 for C#/F# is declared inside A35/A610/A615 but "
            "no dedicated controlling provision pins the single-runtime "
            "invariant"
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
                "law_code": "CODEX_LANGUAGE_SOURCE_NATIVE_SPECIAL_LAW",
                "authority_basis": "A35|A215|A610|A615|A622",
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
                "module_code": "CODEX_MODULE_LANGUAGE",
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
       "codex-amendment-request-shared-dotnet10-runtime-20260926.json")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    json.dump(req, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("written:", out)
