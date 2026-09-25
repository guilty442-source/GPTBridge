import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    # which A6xx exist
    cur = conn.execute(
        "SELECT provision_id, subject FROM articles WHERE provision_id ~ '^A6[0-9][0-9]$' ORDER BY provision_id")
    print("=== A6xx articles:")
    for r in cur.fetchall():
        print(" ", r)

    # formal rules' controlling_provision_ids vs articles
    cur = conn.execute("SELECT DISTINCT controlling_provision_id FROM formal_rule_registry")
    rule_provs = [r[0] for r in cur.fetchall()]
    cur = conn.execute("SELECT provision_id FROM articles")
    have = {r[0] for r in cur.fetchall()}
    print("=== rule provisions missing from articles:", [p for p in rule_provs if p not in have])

    # full text of conflicted articles
    for pid in ("A341", "A347", "A350", "A351", "A353", "A355", "A356", "A359", "A360", "A362", "A604", "A199", "A343", "A35"):
        cur = conn.execute("SELECT subject, rule FROM articles WHERE provision_id=%s", (pid,))
        row = cur.fetchone()
        print(f"\n===== {pid} =====")
        print("subject:", row[0])
        print("rule:", row[1])
