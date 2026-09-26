import sys
sys.path.insert(0, ".")
sys.path.insert(0, "shared-layer/src")
sys.path.insert(0, "main-system/src-core")
from tasks.repair_learning import RepairLearningStore
from pathlib import Path
store = RepairLearningStore(Path("main-system/data/automatic-repair"))
conn = store._connect()
print("search_path check:")
print(conn.execute("SHOW search_path").fetchone())
try:
    print(conn.execute("SELECT COUNT(*) FROM learned_recipes").fetchone())
except Exception as e:
    print("ERR:", e)
conn.close()
