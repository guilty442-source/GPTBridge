import sys
sys.path.insert(0, r"E:\GPTBridge\governance_rule")
from governance_rule.execution.codex_postgresql import readonly_connection

with readonly_connection() as conn:
    for pid in ("B157", "B172", "B166", "D141", "B141"):
        rows = conn.execute(
            "SELECT provision_id, subject, rule, prohibition, exception "
            "FROM articles WHERE provision_id=%s", (pid,)).fetchall()
        for r in rows:
            print(f"===== {r[0]} | {r[1]} =====")
            print("RULE:", r[2])
            print("PROHIBITION:", r[3])
            print("EXCEPTION:", r[4])
            print()
