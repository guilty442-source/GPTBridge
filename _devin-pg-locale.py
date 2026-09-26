import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

def conn_for(port, db):
    a = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
    a["port"] = str(port); a["dbname"] = db
    return psycopg.connect(make_conninfo(**a), autocommit=True)

for p in (5432, 5433):
    c = conn_for(p, "postgres")
    v = c.execute("show server_version").fetchone()[0]
    print("PORT", p, "version", v)
    col = "datlocale"
    for r in c.execute(
        f"select datname, pg_encoding_to_char(encoding), datcollate, datctype, datlocprovider, {col} from pg_database where datname in ('template1','postgres','gptbridge')"):
        print("  ", r)
