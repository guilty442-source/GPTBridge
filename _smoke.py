import io, sys, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
sys.path.insert(0, r"E:\GPTBridge\Standalone tools\local-model\src\backend\services")
from pathlib import Path
TOOL = Path(r"E:\GPTBridge\Standalone tools\local-model")

def run(label, fn):
    try:
        print(f"{label}: OK {fn()}")
    except Exception:
        print(f"{label}: FAIL")
        traceback.print_exc(limit=5)

from xingcheng.infrastructure.repository import LocalAiRepository
for scope in ("main", "investment", "mathematical", "coding"):
    run(f"repo-{scope}", lambda s=scope: LocalAiRepository(TOOL, database_scope=s).database_status())

from xingcheng.infrastructure.transformer_training_repository import TransformerTrainingRepository
def training():
    t = TransformerTrainingRepository(TOOL)
    jobs = t.list_jobs(limit=5) if hasattr(t, "list_jobs") else None
    return f"jobs={len(jobs) if jobs else 0}"
run("training-repo", training)

from xingcheng.infrastructure.local_sqlite_identity_repository import LocalSqliteIdentityRepository
run("identity", lambda: LocalSqliteIdentityRepository(TOOL).health() if hasattr(LocalSqliteIdentityRepository(TOOL), "health") else "no-health")
