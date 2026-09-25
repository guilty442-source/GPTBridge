import sys, json, tempfile
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_amendment_driver import _latest_audit_result
from governance_rule.execution.codex_amendment_executor import execute_amendment, CodexAmendmentDenied

audit = _latest_audit_result("a610-law-classification-20260925")
rec = json.load(open("main-system/runtime/state/codex-amendments/requests/a610-law-classification-20260925.json", encoding="utf-8"))
try:
    res = execute_amendment(
        request_path=rec["request_path"],
        prepared_database="main-system/runtime/state/codex-amendments/candidates/a610-law-classification-20260925.sqlite3",
        audit_result=audit,
        apply=False,
        staging_root=tempfile.mkdtemp(prefix="amend-rehearse-"),
    )
    print("REHEARSAL ok:", res.ok, "| applied:", res.applied, "| version:", res.version)
    for ph in res.phases:
        print(" ", ph.get("phase"), ph.get("ok"), str(ph.get("detail"))[:90])
except CodexAmendmentDenied as e:
    print("DENIED:", e)
