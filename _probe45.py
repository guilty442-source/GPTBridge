import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection
with codex_readonly_connection() as conn:
    keys = ["lang", "startup", "acceptance", "topology", "freeze",
            "typescript", "python", "version", "binding", "closure", "epoch"]
    for r in conn.execute("SELECT key, value FROM metadata ORDER BY key"):
        k = str(r[0]).lower()
        if any(t in k for t in keys):
            print(r[0], "=", str(r[1])[:280])
