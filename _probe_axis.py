import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    for key in (
        "codex_version", "current_version", "current_version_identity",
        "current_version_epoch", "active_provision_binding_version",
        "governance_closure_current_version", "startup_complete_deadline_ms",
        "governance_acceptance_state",
    ):
        r = c.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        print(key, "=", r[0] if r else None)
    print("---missing-article refs---")
    for rc, ref in c.execute(
        "SELECT rule_code, controlling_provision_id FROM formal_rule_registry "
        "WHERE status<>'withdrawn'"
    ):
        ex = c.execute(
            "SELECT 1 FROM articles WHERE provision_id=?", (ref,)
        ).fetchone()
        if not ex:
            print(rc, "->", ref, "MISSING")
    print("---counts---")
    for t in (
        "codex_search_document", "codex_search_fts",
        "codex_internal_module_manifest", "revision_history",
        "seal_manifest", "epoch_seal_manifest",
    ):
        print(t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    print("---revision tail---")
    for r in c.execute(
        "SELECT sequence, change_id, version FROM revision_history "
        "ORDER BY sequence DESC LIMIT 3"
    ):
        print(tuple(r))
    print("---seal tail---")
    for r in c.execute(
        "SELECT version, certification_state FROM seal_manifest "
        "ORDER BY rowid DESC LIMIT 3"
    ):
        print(tuple(r))
