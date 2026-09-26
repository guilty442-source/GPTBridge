"""PostgreSQL 17 -> 18 cutover sync. Runs inside the quiesce window.

Phase 1: fresh dump (parallel dir format, uncompressed) into C:\ProgramData
Phase 2: drop/recreate all DBs on :5433, pg_restore filtered TOC
Phase 3: COPY FROM PROGRAM for the 5 large leaf tables (server-side, fast)
Phase 4: validation vs live :5432 (must match exactly while quiesced)
"""
import subprocess, sys, os, time, re
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

BIN = r"C:\Program Files\PostgreSQL\18\bin"
MIG = r"C:\ProgramData\pg-migrate"
DUMP_DIR = MIG + r"\fresh-dir"
TOC_LIST = MIG + r"\filtered.list"

admin = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
pw = admin["password"]
env = dict(os.environ); env["PGPASSWORD"] = pw; env["LC_ALL"] = "C"

BIG_TABLES = [  # (dumpfile-neutral; resolved via toc), schema, table, dat file
    ("gptbridge_transport", "tool_request"),
    ("gptbridge_xingcheng_main", "market_observation"),
    ("gptbridge_audit", "event"),
    ("gptbridge_xingcheng_investment", "inference_log"),
    ("gptbridge_xingcheng_main", "distribution_event"),
]
SMALL_DBS = [d for d in [
    "gptbridge_module_ai_assistant", "gptbridge_module_ai_collaboration",
    "gptbridge_module_file_sorter", "gptbridge_module_global_cleaner",
    "gptbridge_module_investment_mobile", "gptbridge_module_shared_layer",
    "gptbridge_module_system_rescue", "gptbridge_module_vaultly",
    "gptbridge_module_xingcheng", "gptbridge_scratch",
]]

def run(cmd, **kw):
    t = time.time()
    r = subprocess.run(cmd, env=env, capture_output=True, **kw)
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    print(f"[{time.time()-t:6.1f}s] rc={r.returncode} {' '.join(cmd[:2])} {err[:300]}", flush=True)
    return r, err

def conn_for(port, db):
    a = dict(admin); a["port"] = str(port); a["dbname"] = db
    return psycopg.connect(make_conninfo(**a), autocommit=True)

# ---------- phase 1: dump ----------
print("=== dump ===", flush=True)
os.makedirs(MIG, exist_ok=True)
if os.path.isdir(DUMP_DIR):
    import shutil; shutil.rmtree(DUMP_DIR)
r, err = run([BIN + r"\pg_dump.exe", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres",
              "-Fd", "-j", "4", "-Z", "0", "-f", DUMP_DIR, "gptbridge"])
if r.returncode != 0:
    sys.exit("dump gptbridge failed: " + err[-500:])
subprocess.run(["icacls", DUMP_DIR, "/grant", "Everyone:(OI)(CI)(R)", "/T"],
               capture_output=True)
for db in SMALL_DBS:
    r, err = run([BIN + r"\pg_dump.exe", "-h", "127.0.0.1", "-p", "5432", "-U", "postgres",
                  "-Fc", "-f", MIG + rf"\{db}.dump", db])
    if r.returncode != 0:
        sys.exit(f"dump {db} failed: {err[-300:]}")

# ---------- phase 2: filtered toc + restore ----------
print("=== build filtered toc ===", flush=True)
r = subprocess.run([BIN + r"\pg_restore.exe", "-l", DUMP_DIR], capture_output=True, env=env)
toc = r.stdout.decode("utf-8", errors="replace").splitlines()
excl_patterns = [f"TABLE DATA {s} {t} " for s, t in BIG_TABLES]
filtered = [l for l in toc if not any(p in l for p in excl_patterns)]
open(TOC_LIST, "w", encoding="utf-8").write("\n".join(filtered) + "\n")
print(f"toc items: {len(toc)} -> filtered: {len(filtered)}")

c = conn_for(5433, "postgres")
c.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname='gptbridge' and pid<>pg_backend_pid()")
c.execute('drop database if exists "gptbridge"')
c.execute('create database "gptbridge" owner postgres')
print("recreated gptbridge", flush=True)

print("=== pg_restore (filtered) ===", flush=True)
r, err = run([BIN + r"\pg_restore.exe", "-h", "127.0.0.1", "-p", "5433", "-U", "postgres",
              "-d", "gptbridge", "-j", "4", "-L", TOC_LIST, DUMP_DIR])
print("restore rc:", r.returncode)
if r.returncode != 0:
    print("ERR tail:", err[-1500:])

# ---------- phase 3: server-side COPY for big tables ----------
print("=== COPY FROM PROGRAM ===", flush=True)
# map table -> dat file via toc dumpIds
dat_of = {}
for l in toc:
    for s, t in BIG_TABLES:
        if f"TABLE DATA {s} {t} " in l:
            dumpid = l.split(";")[0].strip()
            dat_of[(s, t)] = DUMP_DIR + "\\" + dumpid + ".dat"
g = conn_for(5433, "gptbridge")
for (s, t), f in dat_of.items():
    tt = time.time()
    g.execute(
        f'copy "{s}"."{t}" from program \'C:\\\\Windows\\\\System32\\\\cmd.exe /c type "{f}"\'')
    n = g.execute(f'select count(*) from "{s}"."{t}"').fetchone()[0]
    print(f"  {s}.{t}: {n} rows in {time.time()-tt:.1f}s", flush=True)

# ---------- module DBs ----------
print("=== module dbs ===", flush=True)
existing = {r0[0] for r0 in c.execute("select datname from pg_database")}
for db in SMALL_DBS:
    c.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname=%s and pid<>pg_backend_pid()", (db,))
    c.execute(f'drop database if exists "{db}"')
    c.execute(f'create database "{db}" owner postgres')
    r, err = run([BIN + r"\pg_restore.exe", "-h", "127.0.0.1", "-p", "5433", "-U", "postgres",
                  "-d", db, MIG + rf"\{db}.dump"])
    print(f"  {db}: rc={r.returncode} {err[:150]}")

# ---------- phase 4: validation ----------
print("=== validation ===", flush=True)
src = conn_for(5432, "gptbridge"); dst = conn_for(5433, "gptbridge")
checks = [
    ("tables", "select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema')"),
    ("tool_request", "select count(*) from gptbridge_transport.tool_request"),
    ("rev_history", "select count(*) from gptbridge_codex.revision_history"),
    ("rev_head", "select entry_hash from gptbridge_codex.revision_history order by sequence desc limit 1"),
    ("authority", "select codex_version from gptbridge_codex.codex_authority_state"),
    ("authority_seq", "select row_count from gptbridge_codex.codex_authority_state"),
]
ok = True
for name, q in checks:
    a0 = src.execute(q).fetchone()[0]; b0 = dst.execute(q).fetchone()[0]
    match = "OK" if a0 == b0 else "MISMATCH"
    if a0 != b0: ok = False
    print(f"  {name}: 17={a0} 18={b0} {match}")
print("RESULT:", "SYNCED" if ok else "DRIFT")
