import sqlite3
conn = sqlite3.connect('governance_rule/codex/data/governance_codex.sqlite3')
cursor = conn.cursor()
cursor.execute('PRAGMA read_only')
print('Read-only pragma:', cursor.fetchone())
try:
    cursor.execute("UPDATE sovereigns SET area='historical' WHERE sovereign_id='system-sub-sovereign'")
    print('Update succeeded')
    conn.commit()
except sqlite3.OperationalError as e:
    print('Error:', e)
conn.close()