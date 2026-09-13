import sqlite3
from pathlib import Path

db_path = Path('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Check for Chinese tables
for row in conn.execute('SELECT name FROM sqlite_master WHERE type="table" AND name LIKE "%chinese%"'):
    print(row['name'])

# Check for Chinese tables
for row in conn.execute('SELECT name FROM sqlite_master WHERE type="table" AND (name LIKE "%chinese%" OR name LIKE "%zh%")'):
    print('ZH table:', row['name'])

conn.close()