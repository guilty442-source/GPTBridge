import json, sys
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_amendment_driver import scan_requests
from governance_rule.execution.codex_amendment_lifecycle import CodexAmendmentRequestLedger
led = CodexAmendmentRequestLedger()
for item in scan_requests():
    st = item.get("state")
    if item.get("valid") and st not in ("executed","rejected","withdrawn"):
        rec = led.load_record(item["request_id"]) or {}
        hist = rec.get("history", [])
        print(item["request_id"], "|", st, "| last:", (hist[-1].get("at") if hist else None), (hist[-1].get("to") if hist else None))
print("--- done ---")
