import sys

sys.path[:0] = [
    r"E:\GPTBridge\governance_rule",
    r"E:\GPTBridge\shared-layer\src",
    r"E:\GPTBridge\main-system\src-core",
]

from governance_rule.execution.codex_repository import (
    codex_readonly_connection,
    CODEX_SCHEMA,
)

with codex_readonly_connection() as con:
    for label, q in {
        "seal_cols": f"SELECT column_name FROM information_schema.columns WHERE table_schema='{CODEX_SCHEMA}' AND table_name='seal_manifest' ORDER BY ordinal_position",
        "rh_cols": f"SELECT column_name FROM information_schema.columns WHERE table_schema='{CODEX_SCHEMA}' AND table_name='revision_history' ORDER BY ordinal_position",
        "seal_rows": f"SELECT * FROM {CODEX_SCHEMA}.seal_manifest ORDER BY version_epoch DESC",
        "rh_tail": f"SELECT * FROM {CODEX_SCHEMA}.revision_history ORDER BY recorded_at_utc DESC LIMIT 4",
    }.items():
        try:
            con.execute("SAVEPOINT sp")
        except Exception:
            pass
        try:
            rows = con.execute(q).fetchall()
            print("---", label)
            for r in rows[:8]:
                print(" ", str(r)[:400])
        except Exception as e:
            print(label, "err:", str(e).splitlines()[0])
        try:
            con.execute("ROLLBACK TO SAVEPOINT sp")
        except Exception:
            pass
