import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    r = c.execute(
        "SELECT * FROM provision_law_classification WHERE provision_id='A610'"
    ).fetchone()
    print("A610 classification:", tuple(r) if r else None)
    for k in ("codex_version", "current_version", "current_version_identity",
              "current_version_epoch"):
        print(k, c.execute(
            "SELECT value FROM metadata WHERE key=?", (k,)).fetchone()[0])
    print("rev tail:", tuple(c.execute(
        "SELECT sequence, change_id, entry_hash FROM revision_history "
        "ORDER BY sequence DESC LIMIT 1").fetchone()))
