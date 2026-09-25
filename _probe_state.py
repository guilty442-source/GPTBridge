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
        "seal_manifest": f"SELECT version, certification_state, history_head, version_epoch, version_identity FROM {CODEX_SCHEMA}.seal_manifest",
        "revision_max": f"SELECT MAX(revision) FROM {CODEX_SCHEMA}.revision_history",
        "revision_cols": f"SELECT column_name FROM information_schema.columns WHERE table_schema='{CODEX_SCHEMA}' AND table_name='seal_manifest' ORDER BY ordinal_position",
        "rh_cols": f"SELECT column_name FROM information_schema.columns WHERE table_schema='{CODEX_SCHEMA}' AND table_name='revision_history' ORDER BY ordinal_position",
    }.items():
        try:
            con.execute("SAVEPOINT sp")
        except Exception:
            pass
        try:
            rows = con.execute(q).fetchall()
            print("---", label)
            for r in rows[-6:]:
                print(" ", str(r)[:250])
        except Exception as e:
            print(label, "err:", str(e).splitlines()[0])
        try:
            con.execute("ROLLBACK TO SAVEPOINT sp")
        except Exception:
            pass
