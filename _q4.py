import sys, json
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_amendment_driver import _latest_audit_result
res = _latest_audit_result("a610-law-classification-20260925")
print("found:", res is not None)
if res:
    print("ok:", res.get("ok"), "| audit_recorded:", res.get("audit_recorded"))
    cert = res.get("certificate") or {}
    print("cert schema:", cert.get("schema"), "| amendment:", cert.get("amendment_id"))
