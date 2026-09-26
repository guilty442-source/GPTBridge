import subprocess, sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg, os

admin = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
admin["port"] = "5433"
admin["dbname"] = "postgres"
pw = admin["password"]

conn = psycopg.connect(make_conninfo(**admin), autocommit=True)
conn.execute(
    "select pg_terminate_backend(pid) from pg_stat_activity where datname='gptbridge' and pid<>pg_backend_pid()")
conn.execute('drop database if exists "gptbridge"')
print("dropped gptbridge")
conn.execute('create database "gptbridge" owner postgres')
print("created gptbridge")
conn.close()

RESTORE = r"C:\Program Files\PostgreSQL\18\bin\pg_restore.exe"
env = dict(os.environ); env["PGPASSWORD"] = pw; env["LC_ALL"] = "C"; env["LANG"] = "C"
dump = r"C:\Users\guilt\AppData\Local\Temp\pg-migrate\gptbridge-dir"
r = subprocess.run(
    [RESTORE, "-h", "127.0.0.1", "-p", "5433", "-U", "postgres",
     "-d", "gptbridge", "-j", "4", "--exit-on-error", dump],
    capture_output=True, env=env)
err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
open(r"C:\Users\guilt\AppData\Local\Temp\pg-migrate\restore-err.log", "w", encoding="utf-8").write(err)
print(f"restore gptbridge: rc={r.returncode}, stderr_lines={err.count(chr(10))+1}")
import collections
cnt = collections.Counter(l.split(":", 2)[-1].strip()[:120] for l in err.splitlines() if l.strip())
for k, v in cnt.most_common(10):
    print(v, "|", k.encode("ascii", errors="backslashreplace").decode())
