import sys

sys.path[:0] = [r"governance_rule", r"shared-layer\src"]
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.cursor()
    for sql in (
        "SELECT * FROM codex_authority_state",
        "SELECT * FROM codex_version_epochs",
    ):
        cur.execute(sql)
        for row in cur.fetchall():
            print(sql.split()[3], "->", row)
        print()
