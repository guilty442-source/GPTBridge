import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection
with codex_readonly_connection() as conn:
    # resolution rows mentioning A406/A407-A410
    for r in conn.execute(
        "SELECT * FROM provision_reference_resolution_v2 WHERE predecessor_id IN "
        "('A406','A407','A408','A409','A410') OR resolved_active_successor_id IN "
        "('A406','A407','A408','A409','A410')"):
        print("refres:", r)
    print()
    # status distribution
    print(conn.execute("SELECT status, COUNT(*) FROM provision_reference_resolution_v2 GROUP BY 1").fetchall())
    # articles text references to superseded provisions: count active articles whose text mentions a superseded provision
    superseded = {r[0] for r in conn.execute(
        "SELECT provision_id FROM provision_lifecycle_status WHERE lifecycle_state='superseded'")}
    active = conn.execute(
        "SELECT a.provision_id, a.rule, a.prohibition, a.exception FROM articles a "
        "JOIN provision_lifecycle_status l ON l.provision_id=a.provision_id "
        "WHERE l.lifecycle_state='active'").fetchall()
    hits = {}
    for pid, rule, proh, exc in active:
        text = f"{rule} {proh} {exc}"
        refs = sorted(s for s in superseded if s in text)
        if refs:
            hits[pid] = refs
    print("active articles referencing superseded:", len(hits))
    for k, v in sorted(hits.items()):
        if any(x in ("A406","A407","A408","A409","A410") for x in v):
            print("  *", k, "->", v)
    for k, v in list(sorted(hits.items()))[:40]:
        print("   ", k, "->", v)
