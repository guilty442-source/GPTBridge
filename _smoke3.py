import io, sys, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
sys.path.insert(0, r"E:\GPTBridge\Standalone tools\local-model\src\backend\services")
from pathlib import Path
TOOL = Path(r"E:\GPTBridge\Standalone tools\local-model")

from xingcheng.infrastructure.native_transformer.self_learning import (
    collect_verified_examples, collect_preference_pairs)
try:
    ex = collect_verified_examples(TOOL)
    pr = collect_preference_pairs(TOOL)
    print(f"self-learning-collect: OK examples={len(ex)} pairs={len(pr)}")
except Exception:
    print("self-learning-collect: FAIL")
    traceback.print_exc(limit=4)
