import subprocess, sys, os
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict

admin = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
pw = admin["password"]
DUMP = r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
out = r"C:\Users\guilt\AppData\Local\Temp\pg-migrate\gptbridge-dir"
env = dict(os.environ); env["PGPASSWORD"] = pw; env["LC_ALL"] = "C"
r = subprocess.run(
    [DUMP, "-h", "127.0.0.1", "-p", "5432", "-U", "postgres",
     "-Fd", "-j", "4", "-Z", "0", "-f", out, "gptbridge"],
    env=env)
print("dump rc:", r.returncode)
