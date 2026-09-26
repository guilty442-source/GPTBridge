import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

admin = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
print("admin dsn keys:", {k: v for k, v in admin.items() if k != "password"})
c = psycopg.connect(make_conninfo(**admin), autocommit=True)
for r in c.execute("select datname, pg_size_pretty(pg_database_size(datname)), datdba::regrole from pg_database order by datname"):
    print("17 db:", r)
for purpose in (DsnPurpose.RUNTIME, DsnPurpose.READER, DsnPurpose.BACKUP):
    p = conninfo_to_dict(resolve_dsn(purpose).dsn)
    print(purpose.value, "->", {k: v for k, v in p.items() if k != "password"})
