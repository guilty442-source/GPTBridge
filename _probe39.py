import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for pid in ("A35","A211","A210","A215","A219","A341","A343","A344","A347","A348","A350","A351","A352","A353","A355","A356","A359","A360","A362","A604"):
        r = conn.execute(
            "SELECT module_code, membership_kind FROM codex_internal_module_membership WHERE provision_id=%s", (pid,)).fetchone()
        print(f"  {pid}: {r}")
    print("\nLANGUAGE module members sample:")
    for r in conn.execute(
        "SELECT provision_id, membership_kind FROM codex_internal_module_membership WHERE module_code='CODEX_MODULE_LANGUAGE' ORDER BY provision_id LIMIT 20"):
        print("  ", r)
