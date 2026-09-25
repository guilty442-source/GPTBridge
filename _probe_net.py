import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    for ver_frag in (".net 8", ".net 9", "dotnet 8", "dotnet 9",
                     "separate clr", "own runtime", "separate runtime"):
        for r in c.execute(
            "SELECT provision_id, rule FROM articles WHERE rule ILIKE %s",
            (f"%{ver_frag}%",)):
            print(ver_frag, "->", r[0], str(r[1])[:160])
    print("--- A615 full rule ---")
    print(c.execute("SELECT rule FROM articles WHERE provision_id='A615'"
                    ).fetchone()[0])
    print("--- A35 runtime fragment ---")
    r = c.execute("SELECT rule FROM articles WHERE provision_id='A35'"
                  ).fetchone()[0]
    i = r.find("RUNTIME-PLATFORM")
    print(r[i:i+300])
