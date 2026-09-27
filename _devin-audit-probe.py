import sys
sys.path.insert(0, r"E:\GPTBridge")
sys.path.insert(0, r"E:\GPTBridge\main-system\src-core")
sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
from pathlib import Path
from governance_rule.execution.audit.native_audit_gate import run_audit_request
native = run_audit_request(Path(r"E:\GPTBridge"))
print("status:", native.status)
print("summary:", native.summary())
for item in getattr(native, "results", []) or []:
    st = getattr(item, "status", "")
    if st and st != "pass":
        print("NONPASS:", st, getattr(item, "check_id", "?"), str(getattr(item, "detail", ""))[:200])
errors = getattr(native, "errors", None) or getattr(native, "error", None)
if errors:
    print("errors:", str(errors)[:2000])
