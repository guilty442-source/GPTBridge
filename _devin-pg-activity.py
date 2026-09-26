import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
import psycopg

conn = psycopg.connect(resolve_dsn(DsnPurpose.ADMIN).dsn, autocommit=True)
rows = conn.execute("""
  select pid, application_name, state, wait_event_type, wait_event,
         now()-query_start as age, left(query, 120)
  from pg_stat_activity
  where datname='gptbridge' and pid<>pg_backend_pid()
  order by query_start
""").fetchall()
for r in rows:
    print(r)
print("---locks not granted---")
for r in conn.execute("""
  select l.pid, l.locktype, l.relation::regclass, l.mode, a.application_name
  from pg_locks l left join pg_stat_activity a on a.pid=l.pid
  where not l.granted""").fetchall():
    print(r)
