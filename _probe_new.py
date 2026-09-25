import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    print("codex_version:", c.execute(
        "SELECT value FROM metadata WHERE key='codex_version'").fetchone()[0])
    print("rev head:", c.execute(
        "SELECT sequence, entry_hash FROM revision_history "
        "ORDER BY sequence DESC LIMIT 1").fetchone())
    print("--- top article numbers ---")
    for r in c.execute(
        "SELECT provision_id FROM articles ORDER BY "
        "CAST(SUBSTRING(provision_id FROM 2) AS int) DESC LIMIT 8"
    ):
        print(" ", r[0])
    print("--- A620 row (template) ---")
    cols = [x[0] for x in c.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='articles' ORDER BY ordinal_position")]
    print(cols)
    row = c.execute(
        "SELECT * FROM articles WHERE provision_id='A620'").fetchone()
    if row:
        for k, v in zip(cols, row):
            print(f"  {k} = {str(v)[:120]}")
