import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    def meta(k):
        r = conn.execute("SELECT value FROM metadata WHERE key=%s", (k,)).fetchone()
        return r[0] if r else None
    print("codex_version:", meta("codex_version"))
    print("current_version:", meta("current_version"))
    print("identity:", meta("current_version_identity"))
    print("binding:", meta("active_provision_binding_version"))
    print("closure:", meta("governance_closure_current_version"))
    print("deadline:", meta("startup_complete_deadline_ms"))
    print("conformance:", meta("startup_deadline_conformance_state"))
    print("lang lifecycle:", meta("language_governance_schema_lifecycle"))
    print("acceptance:", meta("governance_acceptance_state"))
    print("lang roles:", str(meta("canonical_language_roles"))[:400])
    print("topology:", meta("canonical_cross_language_topology"))
    print()
    # formal rules -> missing controlling provisions
    cur = conn.execute(
        "SELECT f.rule_code, f.controlling_provision_id, f.status "
        "FROM formal_rule_registry f LEFT JOIN articles a ON a.provision_id=f.controlling_provision_id "
        "WHERE a.provision_id IS NULL AND f.status<>'withdrawn' ORDER BY f.rule_code")
    print("rules w/ missing article:", cur.fetchall())
    cur = conn.execute(
        "SELECT rule_code, COUNT(*) FROM formal_rule_registry WHERE status<>'withdrawn' "
        "GROUP BY rule_code HAVING COUNT(*)>1")
    print("dup rules:", cur.fetchall())
    cur = conn.execute("SELECT status, COUNT(*) FROM formal_rule_registry GROUP BY status")
    print("rule status:", cur.fetchall())
    # A611-A621 existence + classification
    for aid in ["A610","A611","A612","A613","A614","A615","A616","A617","A618","A619","A620","A621"]:
        a = conn.execute("SELECT provision_id FROM articles WHERE provision_id=%s", (aid,)).fetchone()
        c = conn.execute("SELECT law_code FROM provision_law_classification WHERE provision_type='article' AND provision_id=%s", (aid,)).fetchone()
        l = conn.execute("SELECT lifecycle_state FROM provision_lifecycle_status WHERE provision_type='article' AND provision_id=%s", (aid,)).fetchone()
        m = conn.execute("SELECT module_code FROM codex_internal_module_membership WHERE provision_type='article' AND provision_id=%s", (aid,)).fetchone()
        print(aid, "article:", bool(a), "| cls:", c and c[0], "| lifecycle:", l and l[0], "| module:", m and m[0])
