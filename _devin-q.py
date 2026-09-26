import sys
sys.path.insert(0, 'shared-layer/src')
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
import psycopg

conn = psycopg.connect(resolve_dsn(DsnPurpose.RUNTIME).dsn)
rows = conn.execute(
    "select request_id, status, left(coalesce(response::text,''),90), "
    "claimed_at, lease_until, updated_at "
    "from gptbridge_transport.tool_request "
    "where request_id like 'codex-audit%' order by created_at desc limit 10"
).fetchall()
for r in rows:
    print(r)
print('---queue---')
for r in conn.execute("select status, count(*) from gptbridge_transport.tool_request group by 1"):
    print(r)
