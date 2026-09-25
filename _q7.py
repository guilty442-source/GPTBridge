import sys
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_amendment_driver import _latest_audit_result
from governance_rule.execution import codex_amendment_executor as ex
for rid in ["a610-law-classification-20260925", "cfamily-primary-stack-one-format-20260925-r6", "resource-governor-cpp23-migration-20260925"]:
    audit = _latest_audit_result(rid)
    if audit is None:
        print(rid, "-> no ok audit result"); continue
    try:
        ex._load_audit_result(audit, amendment_id=rid)
        print(rid, "-> GATE_PASS")
    except ex.CodexAmendmentDenied as e:
        print(rid, "-> DENIED:", e)
