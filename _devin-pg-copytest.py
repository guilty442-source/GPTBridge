import sys, time
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import psycopg

a = conninfo_to_dict(resolve_dsn(DsnPurpose.ADMIN).dsn)
a["port"] = "5433"; a["dbname"] = "gptbridge"
c = psycopg.connect(make_conninfo(**a), autocommit=True)

# count current rows, then truncate and reload server-side to measure speed
n0 = c.execute("select count(*) from gptbridge_transport.tool_request").fetchone()[0]
print("rows before:", n0)
dat = r"C:\ProgramData\pg-migrate\8602.dat"
t = time.time()
c.execute("truncate gptbridge_transport.tool_request")
c.execute(
    "copy gptbridge_transport.tool_request from program 'C:\\Windows\\System32\\cmd.exe /c type \""
    + dat + "\"'")
n1 = c.execute("select count(*) from gptbridge_transport.tool_request").fetchone()[0]
print("rows after:", n1, "secs:", round(time.time() - t, 1))
