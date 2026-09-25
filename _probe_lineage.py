import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name='seal_manifest' "
        "ORDER BY ordinal_position"
    )
    cols = [r[0] for r in cur.fetchall()]
    print("seal_manifest cols:", cols)
    order_col = "sealed_at" if "sealed_at" in cols else cols[0]
    cur = conn.execute(
        f"SELECT * FROM seal_manifest ORDER BY {order_col} DESC LIMIT 1"
    )
    print("latest seal:", dict(zip(cols, cur.fetchone())))
    for t in ("revision_history", "codex_authority_state"):
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='gptbridge_codex' AND table_name=%s "
            "ORDER BY ordinal_position", (t,)
        )
        c2 = [r[0] for r in cur.fetchall()]
        print(t, "cols:", c2)
        cur = conn.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        print(t, "rows:", len(rows))
        for r in rows[-2:]:
            print("  ", dict(zip(c2, r)))
