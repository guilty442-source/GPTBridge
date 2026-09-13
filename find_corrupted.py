import sqlite3
from pathlib import Path

db_path = Path('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Find the corrupted sovereign entry
for row in conn.execute('SELECT * FROM provision_law_classification WHERE provision_id LIKE "%\ufffd%"'):
    print(dict(row))

# Also check the Chinese sovereign
for row in conn.execute('SELECT * FROM provision_law_classification WHERE provision_id = "星澄"'):
    print(dict(row))

conn.close()