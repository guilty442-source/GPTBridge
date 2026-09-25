import sys

sys.path[:0] = [r"governance_rule", r"shared-layer\src"]
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT provision_type, provision_id, law_code, tier FROM provision_law_classification WHERE provision_id IN ('A604','A607','A610','A341','A35')"
    )
    for row in cur.fetchall():
        print(row)
    print()
    cur.execute("SELECT DISTINCT law_code, tier FROM provision_law_classification WHERE provision_type='article' ORDER BY 1,2")
    for row in cur.fetchall():
        print(row)
