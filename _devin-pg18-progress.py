import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

a = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
a["port"] = "5433"; a["dbname"] = "gptbridge"
c = psycopg.connect(make_conninfo(**a), autocommit=True)
print(c.execute("select pg_size_pretty(pg_database_size(current_database()))").fetchone())
for r in c.execute("""
  select pid, wait_event_type, wait_event, now()-query_start as age, left(query,90)
  from pg_stat_activity where datname='gptbridge' and state<>'idle'
  order by query_start limit 12"""):
    print(r)
