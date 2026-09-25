import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    print("codex_version:", conn.execute(
        "SELECT value FROM metadata WHERE key='codex_version'").fetchone()[0])

p = r"E:\GPTBridge\main-system\runtime\state\codex-amendments\requests\codex-generation-convergence-20260925.json"
r = json.loads(open(p, encoding="utf-8").read())
print("state:", r.get("state"))
print("history:", [h.get("state") + "@" + str(h.get("at", "")) for h in r.get("history", [])][-6:])
print("exec:", json.dumps(r.get("execution_record"), ensure_ascii=False)[:500])
for h in r.get("history", [])[-3:]:
    print("H:", json.dumps(h, ensure_ascii=False)[:400])
