import sys, sqlite3, json
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_postgresql import export_postgresql_codex
target = export_postgresql_codex("_authority_probe2.sqlite3")
conn = sqlite3.connect(str(target))
rows = conn.execute("select provision_id, subject, rule, prohibition, exception from articles where rule like '%governor%' or subject like '%amendment%' or rule like '%amendment%' or subject like '%executor%' or rule like '%executor%' order by provision_id").fetchall()
for r in rows:
    print("="*15, r[0], r[1], "="*15)
    print("RULE:", r[2][:600])
    print("PROHIB:", (r[3] or "")[:300])
    print("EXCEPT:", (r[4] or "")[:300])
conn.close()
