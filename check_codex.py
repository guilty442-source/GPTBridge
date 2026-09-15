import sqlite3
conn = sqlite3.connect('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Check all sovereigns with their names and areas
cur.execute("SELECT position, sovereign_id, name, area, rank FROM sovereigns ORDER BY position")
for row in cur:
    pos = row['position']
    sid = row['sovereign_id']
    name = row['name']
    area = row['area']
    rank = row['rank']
    # Show both raw and decoded
    print(f"Pos {pos:2d}: sovereign_id={sid!r} ({sid.encode('utf-8')!r})")
    print(f"        name      ={name!r} ({name.encode('utf-8')!r})")
    print(f"        area      ={area}")
    print(f"        rank      ={rank}")
    print()