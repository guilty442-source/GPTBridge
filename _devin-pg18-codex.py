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
    st = c.execute("select codex_version, source_sha256, table_count, row_count, imported_at from gptbridge_codex.codex_authority_state").fetchone()
    print(f"port={port} authority_state={st}")
    rh = c.execute("""
      select sequence, version, entry_hash, recorded_at_utc
      from gptbridge_codex.revision_history
      order by sequence desc limit 3""").fetchall()
    for r in rh:
        print("   rev:", r)
    n = c.execute("select count(*) from gptbridge_codex.revision_history").fetchone()[0]
    print(f"   revision_history rows={n}")
