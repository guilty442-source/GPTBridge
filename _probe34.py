import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    tables = [r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex'")]
    for needle in ("REGISTRY_A521_V1", "RULE_DELEGATED_A498_V1"):
        found = []
        for t in tables:
            cols = [r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
                "AND table_name=%s AND data_type IN ('text','character varying')", (t,))]
            for c in cols:
                try:
                    hit = conn.execute(
                        f"SELECT 1 FROM {t} WHERE {c} = %s LIMIT 1", (needle,)).fetchone()
                    if hit:
                        found.append((t, c))
                except Exception:
                    pass
        print(needle, "->", found)
    # closure_definition_registry columns + count
    c = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='closure_definition_registry' ORDER BY ordinal_position")]
    print("closure_definition_registry cols:", c)
    print("n:", conn.execute("SELECT COUNT(*) FROM closure_definition_registry").fetchone())
    # surface row sample for closure-definition type
    for r in conn.execute(
        "SELECT * FROM current_normative_surface WHERE object_type='closure-definition' LIMIT 3"):
        print("surface closure row:", r)
