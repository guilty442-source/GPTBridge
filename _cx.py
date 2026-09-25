import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection
with codex_readonly_connection() as c:
    tbls = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%amend%' OR name LIKE '%request%' OR name LIKE '%intake%' ORDER BY name").fetchall()]
    print(tbls)
