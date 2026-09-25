import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection
with codex_readonly_connection() as c:
    for pid in ("A610","A211","A264","A195","A501","A348"):
        rows = c.execute("SELECT rule FROM articles WHERE provision_id=%s",(pid,)).fetchall()
        for (r,) in rows:
            print(f"########## {pid}")
            # split on '; ' boundaries for readability
            for seg in r.split(";"):
                print(" ", seg.strip()[:500])
