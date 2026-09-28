import sys
sys.path.insert(0, r"E:\GPTBridge\governance_rule")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    )
    names = [r[0] if not isinstance(r, dict) else list(r.values())[0] for r in cur.fetchall()]
    print(len(names), "tables")
    for n in names:
        if any(k in n for k in ("version", "meta", "article", "rule", "prov")):
            print(" ", n)
