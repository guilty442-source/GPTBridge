import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for pid in ("A351", "A353", "A355", "A356", "A359", "A360"):
        cur = conn.execute("SELECT rule FROM articles WHERE provision_id=%s", (pid,))
        print(f"===== {pid} =====")
        print(cur.fetchone()[0])
        print()
