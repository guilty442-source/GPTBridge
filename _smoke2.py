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
        traceback.print_exc(limit=4)

from xingcheng.infrastructure.transformer_training_repository import TransformerTrainingRepository
def training():
    t = TransformerTrainingRepository(TOOL)
    s = t.database_status()
    return f"jobs={s.get('tables',{}).get('transformer_training_job', s)}"
run("training-repo", training)

from xingcheng.infrastructure.local_sqlite_identity_repository import LocalSqliteIdentityRepository
def ident():
    r = LocalSqliteIdentityRepository(TOOL)
    return "constructed"
run("identity-repo", ident)

from xingcheng.infrastructure.local_sqlite_cognition_repository import LocalSqliteCognitionRepository
def cog():
    r = LocalSqliteCognitionRepository(TOOL)
    return "constructed"
run("cognition-repo", cog)

from xingcheng.infrastructure.native_transformer.self_learning import collect_training_examples
def sl():
    res = collect_training_examples(TOOL)
    return res if not isinstance(res, dict) else {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}
run("self-learning-collect", sl)
