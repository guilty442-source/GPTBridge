import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    print("--- fts provision_type counts:")
    for r in conn.execute("SELECT provision_type, COUNT(*) FROM codex_search_fts GROUP BY 1"):
        print("  ", r)
    print("--- fts vs docs: which docs not in fts?")
    cur = conn.execute(
        "SELECT d.provision_type, COUNT(*) FROM codex_search_document d "
        "WHERE (d.provision_type, d.provision_id) NOT IN "
        "(SELECT provision_type, provision_id FROM codex_search_fts) GROUP BY 1")
    for r in cur.fetchall():
        print("  doc-not-fts:", r)
    # lifecycle of fts members
    cur = conn.execute(
        "SELECT d.lifecycle_state, COUNT(*) FROM codex_search_fts f JOIN codex_search_document d "
        "ON d.provision_type=f.provision_type AND d.provision_id=f.provision_id GROUP BY 1")
    for r in cur.fetchall():
        print("  fts lifecycle:", r)
    # closure-definition source: which table has CLOSURE_DELEGATED ids?
    for t in ("closure_registry", "normative_closure_registry", "closure_contract",
              "module_rule_index_contract", "module_capability_registry", "module_assignment_registry"):
        try:
            c = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
                "AND table_name=%s ORDER BY ordinal_position", (t,))]
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: cols={c} n={n}")
        except Exception as e:
            print(f"  {t}: {type(e).__name__} {str(e)[:80]}")
    # find table containing 'CLOSURE_DELEGATED_A231_V1'
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex'")
    for (t,) in cur.fetchall():
        try:
            hit = conn.execute(f"SELECT 1 FROM {t}::regclass LIMIT 0")
        except Exception:
            pass
    # simpler: search known registries
    for t in ("formal_rule_registry",):
        cur = conn.execute(f"SELECT rule_code FROM {t} WHERE rule_code LIKE 'CLOSURE%' OR rule_code LIKE 'REGISTRY%'")
        print("frr synthetic:", cur.fetchall())
