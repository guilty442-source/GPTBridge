import sys
sys.path.insert(0, ".")
sys.path.insert(0, "shared-layer/src")
from shared_layer.local.pg_adapter import connect
c = connect("gptbridge_repair", autocommit=True)
for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_repair' ORDER BY 1").fetchall():
    print(r[0])
