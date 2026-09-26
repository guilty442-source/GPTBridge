import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

a = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
a["port"] = "5432"; a["dbname"] = "postgres"
c = psycopg.connect(make_conninfo(**a), autocommit=True)
for r in c.execute("""
  select pid, application_name, client_addr, datname, state, backend_start
  from pg_stat_activity
  where pid<>pg_backend_pid() and backend_type='client backend'
  order by pid"""):
    print(r)
