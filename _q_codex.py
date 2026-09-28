import sys
sys.path.insert(0, r"E:\GPTBridge\governance_rule")
from governance_rule.execution.codex_postgresql import readonly_connection

with readonly_connection() as conn:
    # all provision ids; look for concurrency/scheduling subjects
    rows = conn.execute(
        "SELECT provision_id, subject FROM articles ORDER BY provision_id"
    ).fetchall()
    print(len(rows), "articles")
    keys = ("concurren", "queue", "parallel", "schedul", "worker",
            "pool", "backpressure", "deadline", "budget", "thread",
            "cancel", "admission", "resource", "governor", "governance-throttle")
    for pid, subj in rows:
        if any(k in (subj or "").lower() for k in keys):
            print(f"  {pid}: {subj}")
