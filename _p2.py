import json, sys
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_amendment_lifecycle import CodexAmendmentRequestLedger
led = CodexAmendmentRequestLedger()
for rid in ("cfamily-primary-stack-one-format-20260925-r3","native-compute-core-c23-migration-20260925"):
    rec = led.load_record(rid) or {}
    hist = rec.get("history", [])
    last = hist[-1] if hist else {}
    print(rid, "|", rec.get("state"), "|", last.get("at"), last.get("from"), "->", last.get("to"), json.dumps(last.get("evidence",{}), ensure_ascii=False)[:200])
