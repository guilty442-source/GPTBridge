import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for t in ("provision_identities", "provision_lineage", "provision_lifecycle_status",
              "effective_provisions", "articles", "principles", "edicts", "sovereigns"):
        print(t, "count:", conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    # effective_provisions sample + binding staleness
    cur = conn.execute("SELECT * FROM effective_provisions LIMIT 3")
    print("ep sample:", cur.fetchall())
    cur = conn.execute("SELECT current_binding_version, COUNT(*) FROM effective_provisions GROUP BY 1")
    print("ep binding:", cur.fetchall())
    cur = conn.execute("SELECT current_binding_version, COUNT(*) FROM provision_lifecycle_status GROUP BY 1")
    print("lifecycle binding:", cur.fetchall())
    # reference_resolution_v2 cols
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='provision_reference_resolution_v2' ORDER BY ordinal_position")
    print("refres cols:", [r[0] for r in cur.fetchall()])
    for r in conn.execute("SELECT * FROM provision_reference_resolution_v2 LIMIT 3"):
        print("refres:", r)
    # active provisions referencing superseded (text scan approach): count via resolution table
    cur = conn.execute(
        "SELECT COUNT(*) FROM provision_reference_resolution_v2")
    print("refres total:", cur.fetchone())
