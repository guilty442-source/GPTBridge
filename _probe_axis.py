import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    tables = [r[0] for r in c.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public'")]
    hits = []
    for t in tables:
        cols = [x[0] for x in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name=? AND data_type LIKE '%char%'", (t,))]
        for col in cols:
            try:
                n = c.execute(
                    f'SELECT COUNT(*) FROM "{t}" WHERE "{col}" LIKE %s',
                    ("REGISTRY_A52%",)).fetchone()[0]
            except Exception:
                continue
            if n:
                hits.append((t, col, n))
    print(hits)
    # also check formal_rule_registry for RULE_DELEGATED ids
    print(c.execute(
        "SELECT COUNT(*) FROM formal_rule_registry "
        "WHERE rule_code LIKE 'RULE_DELEGATED%'").fetchone()[0])
    print(c.execute(
        "SELECT rule_code, status FROM formal_rule_registry "
        "WHERE rule_code LIKE 'RULE_DELEGATED%'").fetchall())
