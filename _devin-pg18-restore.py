import subprocess, sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

admin = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
admin["port"] = "5433"
admin["dbname"] = "postgres"
pw = admin["password"]

DBS = [
    "gptbridge",
    "gptbridge_module_ai_assistant",
    "gptbridge_module_ai_collaboration",
    "gptbridge_module_file_sorter",
    "gptbridge_module_global_cleaner",
    "gptbridge_module_investment_mobile",
    "gptbridge_module_shared_layer",
    "gptbridge_module_system_rescue",
    "gptbridge_module_vaultly",
    "gptbridge_module_xingcheng",
    "gptbridge_scratch",
]

conn = psycopg.connect(make_conninfo(**admin), autocommit=True)
existing = {r[0] for r in conn.execute("select datname from pg_database")}
print("existing:", existing)
for db in DBS:
    if db in existing:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s and pid<>pg_backend_pid()",
            (db,))
        conn.execute(f'drop database "{db}"')
        print("dropped", db)
    conn.execute(f'create database "{db}" owner postgres')
    print("created", db)
conn.close()

import os
os.environ["PYTHONIOENCODING"] = "utf-8"

RESTORE = r"C:\Program Files\PostgreSQL\18\bin\pg_restore.exe"
env = dict(__import__("os").environ); env["PGPASSWORD"] = pw
for db in DBS:
    dump = rf"C:\Users\guilt\AppData\Local\Temp\pg-migrate\{db}.dump"
    r = subprocess.run(
        [RESTORE, "-h", "127.0.0.1", "-p", "5433", "-U", "postgres",
         "-d", db, "-j", "2", dump],
        capture_output=True, env=env)
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    print(f"restore {db}: rc={r.returncode} err={err[:200]}", flush=True)
