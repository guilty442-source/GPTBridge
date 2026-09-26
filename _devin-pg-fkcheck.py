import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

a = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
a["port"] = "5433"; a["dbname"] = "gptbridge"
c = psycopg.connect(make_conninfo(**a), autocommit=True)

targets = [
    ("gptbridge_transport", "tool_request"),
    ("gptbridge_xingcheng_main", "market_observation"),
    ("gptbridge_audit", "event"),
    ("gptbridge_maintenance", "maintenance_jobs"),
    ("gptbridge_xingcheng_investment", "inference_log"),
    ("gptbridge_xingcheng_main", "distribution_event"),
]
print("--- inbound FKs (other table -> big table) ---")
for s, t in targets:
    rows = c.execute("""
      select distinct conrelid::regclass::text
      from pg_constraint
      where contype='f' and confrelid=%s::regclass""",
      (f"{s}.{t}",)).fetchall()
    print(f"{s}.{t}: {rows}")
print("--- outbound FKs ---")
for s, t in targets:
    rows = c.execute("""
      select confrelid::regclass::text
      from pg_constraint
      where contype='f' and conrelid=%s::regclass""",
      (f"{s}.{t}",)).fetchall()
    print(f"{s}.{t}: {rows}")
