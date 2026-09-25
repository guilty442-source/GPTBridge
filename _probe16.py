import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    classified = {
        (str(r[0]), str(r[1]))
        for r in conn.execute("SELECT provision_type, provision_id FROM provision_law_classification")
    }
    expected = set()
    for table, ptype, col in (("principles","principle","provision_id"),
                              ("articles","article","provision_id"),
                              ("edicts","edict","provision_id"),
                              ("sovereigns","sovereign","sovereign_id")):
        expected |= {(ptype, str(r[0])) for r in conn.execute(f"SELECT {col} FROM {table}")}
    missing = expected - classified
    extra = classified - expected
    print("MISSING classifications:", sorted(missing))
    print("EXTRA classifications:", sorted(extra))
    # module manifest-ish tables
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex' "
        "AND (table_name LIKE '%module%' OR table_name LIKE '%manifest%' OR table_name LIKE '%surface%' "
        "OR table_name LIKE '%epoch%' OR table_name LIKE '%search%') ORDER BY table_name")
    print("=== manifest/module/epoch/search/surface tables:")
    for r in cur.fetchall():
        print("  ", r[0])
    # current authority
    cur = conn.execute("SELECT value FROM metadata WHERE key='codex_version'")
    print("codex_version =", cur.fetchone())
