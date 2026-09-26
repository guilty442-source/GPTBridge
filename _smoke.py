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

from xingcheng.infrastructure.repository import LocalAiRepository
for scope in ("main", "investment", "mathematical", "coding"):
    run(f"repo-{scope}", lambda s=scope: LocalAiRepository(TOOL, database_scope=s).database_status())
