import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

IDS = ["A199","A219","A341","A343","A347","A350","A351","A353","A356","A359","A360","A362","A604","A610"]
with codex_readonly_connection() as conn:
    for aid in IDS:
        r = conn.execute(
            "SELECT provision_id, subject, rule, prohibition, exception FROM articles WHERE provision_id=%s", (aid,)).fetchone()
        if not r:
            print(aid, "MISSING"); continue
        print("="*100)
        print(aid, "|", r[1])
        print("RULE:", r[2][:1800])
        print("PROHIB:", r[3][:400])
        print("EXC:", r[4][:300])
    # distinct membership_kind + module codes for language articles
    print("="*100)
    print("membership kinds:", conn.execute(
        "SELECT DISTINCT membership_kind FROM codex_internal_module_membership").fetchall())
    print("A343 membership:", conn.execute(
        "SELECT * FROM codex_internal_module_membership WHERE provision_id='A343'").fetchall())
    print("A343 cls:", conn.execute(
        "SELECT * FROM provision_law_classification WHERE provision_id='A343'").fetchall())
    # obligations text needing correction
    print("="*100)
    cur = conn.execute("SELECT obligation_code, declaration_provision, current_state, target_state FROM implementation_obligations")
    for row in cur.fetchall():
        print("OBL:", row)
