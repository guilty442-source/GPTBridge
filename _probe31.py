import sys, io, itertools
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import search_document_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    def cols(t):
        return [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
            "AND table_name=%s ORDER BY ordinal_position", (t,))]
    # edicts
    print("edicts cols:", cols("edicts"))
    e = conn.execute("SELECT * FROM edicts WHERE provision_id='E1'").fetchone()
    print("edict E1:", e)
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='E1'").fetchone()
    print("doc:", repr(d[0]))
    # principles
    print("principles cols:", cols("principles"))
    p = conn.execute("SELECT * FROM principles WHERE provision_id='P70'").fetchone()
    print("principle P70:", p)
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='P70'").fetchone()
    print("doc:", repr(d[0]))
    # sovereigns
    print("sovereigns cols:", cols("sovereigns"))
    s = conn.execute("SELECT * FROM sovereigns WHERE sovereign_id='automatic-log-sync-sub-sovereign'").fetchone()
    print("sovereign:", s)
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='automatic-log-sync-sub-sovereign'").fetchone()
    print("doc:", repr(d[0]))
    # closure-definition + registry-rule docs — what are they?
    for pt in ("closure-definition", "registry-rule", "formal-rule"):
        d = conn.execute(
            "SELECT provision_id, subject, content FROM codex_search_document WHERE provision_type=%s LIMIT 2", (pt,))
        for row in d.fetchall():
            print(f"### {pt} {row[0]} subj={row[1]}")
            print("   ", repr(row[2][:200]))
    # sections/preamble docs?
    for r in conn.execute("SELECT provision_type, provision_id FROM codex_search_document WHERE provision_type='article' AND provision_id LIKE 'S%' LIMIT 5"):
        print("S-doc:", r)
