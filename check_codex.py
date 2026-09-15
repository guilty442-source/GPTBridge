import sqlite3
conn = sqlite3.connect('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Check sovereign_hierarchy_registry
cur.execute("SELECT * FROM sovereign_hierarchy_registry")
for row in cur:
    print(dict(row))