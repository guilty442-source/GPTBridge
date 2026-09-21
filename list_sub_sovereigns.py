import sqlite3
conn = sqlite3.connect('governance_rule/codex/data/governance_codex.sqlite3')
cursor = conn.cursor()
cursor.execute("SELECT sovereign_id FROM sovereigns WHERE sovereign_id LIKE '%sub_sovereign%' OR sovereign_id LIKE '%sub-sovereign%' OR sovereign_id LIKE '%identity-group%'")
rows = cursor.fetchall()
for row in rows:
    print(row[0])
conn.close()