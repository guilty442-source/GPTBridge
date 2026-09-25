import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    tables = [r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex'")]
    # find which table has a column holding 'CLOSURE_DELEGATED_A231_V1'
    found = []
    for t in tables:
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
            "AND table_name=%s AND data_type IN ('text','character varying')", (t,))]
        for c in cols:
            try:
                hit = conn.execute(
                    f"SELECT 1 FROM {t} WHERE {c} = 'CLOSURE_DELEGATED_A231_V1' LIMIT 1").fetchone()
                if hit:
                    found.append((t, c))
            except Exception:
                pass
    print("CLOSURE_DELEGATED_A231_V1 found in:", found)
