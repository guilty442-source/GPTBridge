import sys

sys.path[:0] = [r"governance_rule", r"shared-layer\src"]
from governance_rule.execution.codex_repository import codex_readonly_connection

def q(sql, params=()):
    with codex_readonly_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

with codex_readonly_connection() as conn:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = sorted(r[0] for r in cur.fetchall())
print("tables:", len(tables))
for t in tables:
    if any(k in t for k in ("isolat", "quota", "tool", "resource")):
        print("  ", t)

print("\n== metadata language/format/process keys ==")
for row in q("SELECT key, value FROM metadata"):
    k = str(row[0])
    if any(s in k for s in ("process", "isolation", "format", "language", "cfamily", "csharp")):
        print("  ", k, "=", str(row[1])[:140])
