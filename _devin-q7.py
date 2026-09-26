import sys, traceback
sys.path.insert(0, ".")
sys.path.insert(0, "shared-layer/src")
sys.path.insert(0, "main-system/src-core")
from tasks.repair_learning import RepairLearningStore, RepairLearner
from pathlib import Path
store = RepairLearningStore(Path("main-system/data/automatic-repair"))
learner = RepairLearner(store)
try:
    r = learner.teach_recipe(name="_devin_probe", failure_signatures=("X","Y"), remedy="inspect-owned-databases")
    print("teach:", r)
except Exception:
    traceback.print_exc()
