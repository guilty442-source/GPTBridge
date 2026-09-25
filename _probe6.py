import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

def cols(conn, t):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name=%s ORDER BY ordinal_position", (t,))
    return [r[0] for r in cur.fetchall()]

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex' ORDER BY table_name")
    tables = [r[0] for r in cur.fetchall()]
    for key in ("lifecycle", "identit", "supersession", "provision"):
        print(f"-- '{key}':", [t for t in tables if key in t])

    for t in ("provision_identities", "provision_lifecycle_status", "provision_supersession_edges", "supersession_registry"):
        if t in tables:
            c = cols(conn, t)
            cur = conn.execute(f"SELECT * FROM {t}")
            rows = cur.fetchall()
            print(f"=== {t} cols={c} rows={len(rows)}")
            for r in rows[:4]:
                print("   ", {k: str(v)[:60] for k, v in zip(c, r)})

    # superseded provision set
    if "provision_lifecycle_status" in tables:
        c = cols(conn, "provision_lifecycle_status")
        cur = conn.execute("SELECT * FROM provision_lifecycle_status")
        rows = cur.fetchall()
        st_col = "lifecycle_state" if "lifecycle_state" in c else ("status" if "status" in c else c[1])
        pid_col = "provision_id" if "provision_id" in c else c[0]
        from collections import Counter
        print("lifecycle counts:", Counter(r[c.index(st_col)] for r in rows))
        superseded = {r[c.index(pid_col)] for r in rows if "supersed" in str(r[c.index(st_col)]).lower()}
        print("superseded count:", len(superseded), "sample:", sorted(superseded)[:10])
