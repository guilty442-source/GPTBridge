import sqlite3
from pathlib import Path

db_path = Path('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Check tables
for row in conn.execute('SELECT name FROM sqlite_master WHERE type="table" ORDER BY name'):
    print(row['name'])

print()
# Check articles table schema
for row in conn.execute('PRAGMA table_info(articles)'):
    print(dict(row))

print()
# Check if article_chinese exists
for row in conn.execute('PRAGMA table_info(article_chinese)'):
    print(dict(row))

conn.close()