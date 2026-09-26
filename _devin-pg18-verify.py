import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

def conn_for(port, db):
    a = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
    a["port"] = str(port); a["dbname"] = db
    return psycopg.connect(make_conninfo(**a), autocommit=True)

for port in (5432, 5433):
    c = conn_for(port, "gptbridge")
    v = c.execute("show server_version").fetchone()[0]
    size = c.execute("select pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
    ntbl = c.execute("select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema')").fetchone()[0]
    tr = c.execute("select count(*) from gptbridge_transport.tool_request").fetchone()[0]
    print(f"port={port} version={v} size={size} tables={ntbl} tool_request={tr}")

c = conn_for(5433, "gptbridge")
print("per-schema counts (18):")
for r in c.execute("""
  select schemaname, count(*) from pg_tables
  where schemaname not in ('pg_catalog','information_schema')
  group by schemaname order by schemaname"""):
    print(" ", r)

# codex authority check on the codex DB if present
try:
    cc = conn_for(5433, "gptbridge_codex")
    print("codex on 18:", cc.execute("select version, revision_sequence from codex_authority_state").fetchall())
except Exception as e:
    print("codex db check:", e)
