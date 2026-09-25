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

article = {
    "position": 622,
    "provision_id": "A622",
    "section_index": "7",
    "subject": "adaptive-format-toolchain-sharing",
    "rule": (
        "ADAPTIVE-FORMAT-TOOLCHAIN:file format adaptation is a performance-"
        "critical path: every format adapter uses the highest-performance "
        "implementation its layer allows (compiled/streaming/zero-copy "
        "preferred over interpreted/eager-buffered); compiler and interpreter "
        "selection is adaptive to the layer's declared performance class -- "
        "compiled runtimes own hot paths, interpreted execution is confined "
        "to Python's bounded governance domain and explicit boundary "
        "adapters; implementations that can be shared are shared: one "
        "canonical adapter/codec/toolchain per format per layer, reused by "
        "every consumer, vendored offline toolchains build missing binaries "
        "in place instead of duplicating them."
    ),
    "prohibition": (
        "FORBID:interpreted format parsing on a hot path|per-tool duplicate "
        "adapter for the same format|non-shared toolchain when a shared one "
        "exists|eager full-buffer parsing where streaming suffices|network "
        "fetch of a toolchain that has a vendored copy"
    ),
    "exception": (
        "bounded governance Python semantics may use interpreted adapters; "
        "grandfathered per-tool adapters remain legal until migrated to the "
        "shared implementation"
    ),
}

insert = {"action": "insert"}
req = {
    "artifact": "codex-amendment-request",
    "authority": "request-only",
    "schema": "codex-amendment-request/v1",
    "request_id": "adaptive-format-toolchain-sharing-20260926",
    "title": "New article A622: adaptive format/toolchain, shared implementations",
    "summary": (
        "Adds article A622 codifying three directives: (1) file format "
        "adaptation must meet the high-performance bar of its layer; (2) "
        "compiler vs interpreter selection adapts to the layer's performance "
        "class; (3) shareable adapters/codecs/toolchains are shared canonical "
        "implementations rather than per-tool duplicates. Classified under "
        "CODEX_LANGUAGE_SOURCE_NATIVE_SPECIAL_LAW (special-law tier), module "
        "CODEX_MODULE_LANGUAGE."
    ),
    "requested_by": "decision-sovereign",
    "origin": "user directive: format adaptation high-performance; compiler/interpreter adaptive; share what is shareable",
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
            "no codified rule governs format-adapter performance class, "
            "adaptive compiler/interpreter selection, or shared adapter/"
            "toolchain reuse"
        )
    },
    "proposed_successors": [
        {"registry": "articles", "action": "insert", "rows": [article]},
        {
            "registry": "provision_law_classification",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": "A622",
                "tier": "special-law",
                "law_code": "CODEX_LANGUAGE_SOURCE_NATIVE_SPECIAL_LAW",
                "authority_basis": "A215|A341|A615|A620",
            }],
        },
        {
            "registry": "provision_lifecycle_status",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": "A622",
                "lifecycle_state": "active",
                "effective_version": "<successor-version>",
                "successor_identity": None,
                "evidence": "A622-current",
                "legacy_effective_version": None,
                "current_binding_version": "<successor-version>",
            }],
        },
        {
            "registry": "codex_internal_module_membership",
            "action": "insert",
            "rows": [{
                "provision_type": "article",
                "provision_id": "A622",
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
                "provision_id": "A622",
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
            "search/FTS/manifest projections rebuilt for successor generation"
        ),
    },
}

out = ("E:/GPTBridge/governance_rule/execution/audit/convergence/"
       "codex-amendment-request-adaptive-format-toolchain-sharing-20260926.json")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    json.dump(req, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("predecessor:", ver, ident, "epoch", epoch, "seq", seq)
print("written:", out)
