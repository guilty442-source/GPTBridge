import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
import psycopg

conn = psycopg.connect(resolve_dsn(DsnPurpose.ADMIN).dsn, autocommit=True)
print("---pg_stat_progress_copy---")
for r in conn.execute("""
  select pid, relid::regclass, bytes_processed, bytes_total, tuples_processed
  from pg_stat_progress_copy""").fetchall():
    print(r)
print("---analyze progress---")
for r in conn.execute("""
  select pid, relid::regclass, phase from pg_stat_progress_analyze""").fetchall():
    print(r)
