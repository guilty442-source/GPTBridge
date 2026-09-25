import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    print("--- machine_schema_registry status/parity_status:")
    for r in conn.execute(
        "SELECT status, parity_status, COUNT(*) FROM machine_schema_registry GROUP BY 1,2 ORDER BY 1,2"):
        print("  ", r)
    print("--- schemas in registry lacking parity evidence rows:")
    cur = conn.execute(
        "SELECT r.schema_code, r.status, r.parity_status FROM machine_schema_registry r "
        "WHERE r.schema_code NOT IN (SELECT schema_code FROM machine_schema_parity_evidence) ORDER BY 1")
    rows = cur.fetchall()
    print("   count:", len(rows))
    for r in rows[:40]:
        print("  ", r)
    print("\n--- codex_search_document version_identity counts:")
    for r in conn.execute(
        "SELECT version_identity, COUNT(*) FROM codex_search_document GROUP BY 1 ORDER BY 1"):
        print("  ", r)
    print("--- codex_version now:", conn.execute("SELECT value FROM metadata WHERE key='codex_version'").fetchone())

# what executed at 15:42 -> check ledger
import glob, os
req_dir = r"main-system\runtime\state\codex-amendments\requests"
files = sorted(glob.glob(os.path.join(req_dir, "*.json")), key=os.path.getmtime)
for f in files[-6:]:
    d = json.load(open(f, encoding="utf-8"))
    print(os.path.basename(f), "->", d.get("state"), d.get("version"))
