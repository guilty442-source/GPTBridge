import sys, json
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

IDS = ["A199", "A219", "A347", "A350", "A351", "A353", "A355", "A356",
       "A359", "A360", "A362", "A604", "A35", "A341", "A343", "A210", "A211",
       "A407", "A408", "A409", "A410", "A411", "A406", "A493", "A412",
       "A609", "A610"]
with codex_readonly_connection() as conn:
    for pid in IDS:
        cur = conn.execute(
            "SELECT position, section_index, subject, rule, prohibition, exception "
            "FROM articles WHERE provision_id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            print(f"### {pid}: NOT FOUND"); continue
        print(f"### {pid} pos={row[0]} sec={row[1]} subj={row[2]}")
        print(f"  RULE: {row[3]}")
        print(f"  PROH: {row[4]}")
        print(f"  EXC : {row[5]}")
    # formal rule -> provision refs
    cur = conn.execute("SELECT rule_code, provision_ref FROM formal_rule_registry ORDER BY rule_code")
    print("\n=== formal_rule_registry provision refs:")
    for r in cur.fetchall():
        print(f"  {r[0]} -> {r[1]}")
