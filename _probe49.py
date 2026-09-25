import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection
with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='supersession_registry' ORDER BY ordinal_position")
    cols = [c[0] for c in cur.fetchall()]
    print("supersession cols:", cols)
    for r in conn.execute("SELECT * FROM supersession_registry LIMIT 5"):
        print("sup:", r)
    # A406-A411 references in text
    for aid in ["A407", "A408", "A409", "A410", "A411", "A493"]:
        r = conn.execute(
            "SELECT subject, rule FROM articles WHERE provision_id=%s", (aid,)).fetchone()
        if r:
            import re
            m = [s for s in ("A399","A394","A396","A397","A398","A406") if s in r[1]]
            print(aid, r[0], "| mentions:", m, "| text head:", r[1][:160])
