import os
import sys
import psycopg

conn = psycopg.connect(os.environ["GPTBRIDGE_POSTGRES_DSN"])
conn.execute("SET search_path TO gptbridge_codex")

terms = sys.argv[1] if len(sys.argv) > 1 else "queue"
# search document text via ILIKE (zh terms and en terms both)
rows = conn.execute(
    "SELECT DISTINCT provision_type, provision_id, module_code, "
    "substring(content, 1, 300) FROM codex_search_document "
    "WHERE content ILIKE %s ORDER BY provision_id LIMIT 30",
    (f"%{terms}%",),
).fetchall()
for r in rows:
    print("=" * 70)
    print(f"{r[0]} {r[1]} [{r[2]}]")
    print(r[3])
