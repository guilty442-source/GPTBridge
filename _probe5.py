import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

def cols(conn, t):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name=%s ORDER BY ordinal_position", (t,))
    return [r[0] for r in cur.fetchall()]

with codex_readonly_connection() as conn:
    # FSharp analysis/ML references in active articles
    cur = conn.execute("SELECT provision_id, rule FROM articles")
    for pid, rule in cur.fetchall():
        rl = str(rule)
        if ("fsharp" in rl.lower() or "f#" in rl.lower()) and ("analysis" in rl.lower() or "machine learning" in rl.lower() or " ml" in rl.lower()):
            print("F#-analysis ref:", pid, "|", rl[:160])

    # metadata 40000
    cur = conn.execute("SELECT key, value FROM metadata")
    for k, v in cur.fetchall():
        if "40000" in str(v) or "deadline" in k.lower() or "startup" in k.lower():
            print("metadata:", k, "=", str(v)[:120])

    # provision lifecycle/status tables
    cur = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex' AND (table_name LIKE '%lifecycle%' OR table_name LIKE '%status%' OR table_name LIKE '%identit%')")
    for (t,) in cur.fetchall():
        c = cols(conn, t)
        n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print("table:", t, c, "rows:", n)

    # provision_identities vs law classification
    for t in ("provision_identities", "provision_lifecycle_status"):
        try:
            c = cols(conn, t)
            cur = conn.execute(f"SELECT * FROM {t} LIMIT 3")
            print(f"=== {t}: {c}")
            for r in cur.fetchall():
                print("   ", {k: str(v)[:60] for k, v in zip(c, r)})
        except Exception as e:
            print(t, "ERR", e)
            conn2 = None
