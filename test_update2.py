import sqlite3
conn = sqlite3.connect('governance_rule/codex/data/governance_codex.sqlite3', isolation_level=None)
cursor = conn.cursor()
try:
    cursor.execute('BEGIN IMMEDIATE')
    print('Got immediate lock')
    cursor.execute("UPDATE sovereigns SET area='historical' WHERE sovereign_id='system-sub-sovereign'")
    print('Update succeeded')
    cursor.execute('COMMIT')
except sqlite3.OperationalError as e:
    print('Error:', e)
finally:
    conn.close()