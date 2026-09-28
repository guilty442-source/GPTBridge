import sys
sys.path.insert(0, r"E:\GPTBridge\governance_rule")
from governance_rule.execution.codex_postgresql import readonly_connection

with readonly_connection() as conn:
    for pid in ("C59", "C96", "C27", "C26"):
        for r in conn.execute(
            "SELECT provision_id, rule, prohibition FROM articles "
            "WHERE provision_id=%s", (pid,)).fetchall():
            print(f"===== {r[0]}")
            print("RULE:", r[1])
            print("PROHIBIT:", (r[2] or "")[:600])
            print()
    # connection pool contract table
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' "
        "AND table_name='sql_connection_pool_contract'").fetchall()
    print("pool contract cols:", [c[0] for c in cols])
    rows = conn.execute("SELECT * FROM sql_connection_pool_contract").fetchall()
    for r in rows[:10]:
        print(dict(zip([c[0] for c in cols], r)) if rows else "")
