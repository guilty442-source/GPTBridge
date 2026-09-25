import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex' ORDER BY table_name")
    names = [r[0] for r in cur.fetchall()]
    for n in names:
        if any(k in n for k in ("module", "manifest", "surface", "epoch", "search", "obligation", "schema", "supersession", "reference")):
            print("  ", n)
    cur = conn.execute("SELECT value FROM metadata WHERE key='codex_version'")
    print("codex_version =", cur.fetchone()[0])
