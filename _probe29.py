import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import search_document_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for ptype in ("principle", "edict", "sovereign"):
        r = conn.execute(
            "SELECT provision_type, provision_id, subject, content FROM codex_search_document "
            "WHERE provision_type=%s LIMIT 1", (ptype,)).fetchone()
        print(f"### {ptype} {r[1]} subj={r[2]}")
        print(r[3][:400])
        print("---tail:", r[3][-120:])
        print()
    # count docs per type
    for r in conn.execute("SELECT provision_type, COUNT(*) FROM codex_search_document GROUP BY 1"):
        print(r)
    # any docs for non-content provisions? e.g. articles lifecycle
    for r in conn.execute("SELECT lifecycle_state, COUNT(*) FROM codex_search_document GROUP BY 1"):
        print(r)
    # how were A610-era docs handled (do docs exist for A610/A612...)?
    cur = conn.execute("SELECT provision_id FROM codex_search_document WHERE provision_id IN ('A610','A612','A615','A616','A621')")
    print("docs for new articles:", cur.fetchall())
    # does the doc table even include superseded provisions? A35 shows lc=superseded - yes all provisions
