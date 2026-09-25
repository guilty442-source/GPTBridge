import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='gptbridge_codex' AND table_name LIKE 'codex_internal%' "
        "ORDER BY table_name")
    for (t,) in cur.fetchall():
        n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(t, n)

    # module membership source
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name='codex_internal_module_directory' ORDER BY ordinal_position")
    print("module_directory cols:", [r[0] for r in cur.fetchall()])
    cur = conn.execute("SELECT * FROM codex_internal_module_directory LIMIT 3")
    c = [x[0] for x in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' AND table_name='codex_internal_module_directory' ORDER BY ordinal_position").fetchall()]
    for r in cur.fetchall():
        print("   ", {k: str(v)[:80] for k, v in zip(c, r)})
