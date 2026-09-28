import sys
sys.path.insert(0, r"E:\GPTBridge\governance_rule")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.execute(
        "SELECT version, created_at FROM codex_versions ORDER BY created_at DESC LIMIT 6"
    )
    for row in cur.fetchall():
        print(dict(row) if isinstance(row, dict) else row)
