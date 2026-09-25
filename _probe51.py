import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

V = "2026-09-25T17:52:49Z"
with codex_readonly_connection() as conn:
    def meta(k):
        r = conn.execute("SELECT value FROM metadata WHERE key=%s", (k,)).fetchone()
        return r[0] if r else None
    for k in ("codex_version", "current_version", "current_version_identity",
              "active_provision_binding_version", "governance_closure_current_version",
              "startup_complete_deadline_ms", "startup_deadline_conformance_state",
              "language_governance_schema_lifecycle", "governance_acceptance_state",
              "canonical_language_roles", "canonical_cross_language_topology"):
        print(k, "=", str(meta(k))[:200])
    print()
    print("docs@V:", conn.execute(
        "SELECT COUNT(*) FROM codex_search_document WHERE version_identity=%s", (V,)).fetchone())
    print("manifest:", conn.execute(
        "SELECT codex_version_identity,status FROM codex_search_index_manifest").fetchall())
    print("mm:", conn.execute(
        "SELECT version_identity, COUNT(*) FROM codex_internal_module_manifest GROUP BY 1").fetchall())
    print("surface:", conn.execute(
        "SELECT version_identity, COUNT(*) FROM current_normative_surface GROUP BY 1").fetchall())
    print("rev tail:", conn.execute(
        "SELECT sequence, change_id, version FROM revision_history ORDER BY sequence DESC LIMIT 3").fetchall())
    print("seal tail:", conn.execute(
        "SELECT version, certification_state FROM seal_manifest ORDER BY rowid DESC LIMIT 3").fetchall())
    print("epoch seal:", conn.execute(
        "SELECT version, version_identity FROM epoch_seal_manifest ORDER BY rowid DESC LIMIT 3").fetchall())
    print("parity:", conn.execute(
        "SELECT status, COUNT(*) FROM machine_schema_parity_evidence GROUP BY status").fetchall())
    print("dangling rules:", conn.execute(
        "SELECT COUNT(*) FROM formal_rule_registry f LEFT JOIN articles a "
        "ON a.provision_id=f.controlling_provision_id WHERE a.provision_id IS NULL "
        "AND f.status<>'withdrawn'").fetchone())
    print("A199 40000:", conn.execute(
        "SELECT COUNT(*) FROM articles WHERE rule LIKE '%40000%' OR prohibition LIKE '%40000%'").fetchone())
    print("lifecycle binding:", conn.execute(
        "SELECT current_binding_version, COUNT(*) FROM provision_lifecycle_status GROUP BY 1").fetchall())
