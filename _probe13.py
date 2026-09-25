import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT rule_code, controlling_provision_id, version_identity, status FROM formal_rule_registry "
        "WHERE status<>'withdrawn' ORDER BY rule_code")
    print("=== active rules -> controlling_provision_id:")
    for r in cur.fetchall():
        print("  ", r)
    print()
    for pid in ["A612", "A615", "A616", "A621"]:
        cur = conn.execute(
            "SELECT subject, rule FROM articles WHERE provision_id=%s", (pid,))
        row = cur.fetchone()
        print(f"### {pid} subj={row[0] if row else 'MISSING'}")
        if row:
            print(f"  RULE: {row[1][:500]}")
        cur = conn.execute(
            "SELECT provision_type, tier, law_code FROM provision_law_classification WHERE provision_id=%s", (pid,))
        print("  cls:", cur.fetchall())
        cur = conn.execute(
            "SELECT lifecycle_state, version_identity FROM provision_lifecycle_status WHERE provision_id=%s", (pid,))
        print("  lc :", cur.fetchall())
    # which provisions do rules reference but missing from articles?
    cur = conn.execute("SELECT DISTINCT controlling_provision_id FROM formal_rule_registry WHERE status<>'withdrawn'")
    refs = {r[0] for r in cur.fetchall()}
    cur = conn.execute("SELECT provision_id FROM articles")
    arts = {r[0] for r in cur.fetchall()}
    print("\nmissing article refs:", sorted(refs - arts))
    # check articles exist for each missing ref's law_classification anyway
    cur = conn.execute(
        "SELECT provision_id FROM provision_law_classification WHERE provision_id IN "
        "(SELECT DISTINCT controlling_provision_id FROM formal_rule_registry WHERE status<>'withdrawn')")
    print("rules' provisions having law_classification:", len(cur.fetchall()))
