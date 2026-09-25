import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

TABLES = ["articles", "provision_law_classification", "provision_lifecycle_status",
          "codex_internal_module_membership", "provision_identities", "provision_lineage",
          "effective_provisions", "implementation_obligations",
          "machine_schema_parity_evidence", "machine_schema_registry",
          "architecture_stale_reference_evidence"]
with codex_readonly_connection() as conn:
    for t in TABLES:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
            "AND table_name=%s ORDER BY ordinal_position", (t,))
        print(t, "->", [r[0] for r in cur.fetchall()])
    print()
    # template rows for A609 (a fully-registered recent article) across tables
    aid = "A609"
    for t, where in [
        ("articles", "provision_id"),
        ("provision_lifecycle_status", "provision_id"),
        ("codex_internal_module_membership", "provision_id"),
        ("provision_law_classification", "provision_id"),
        ("provision_identities", "provision_id"),
        ("effective_provisions", "provision_id"),
    ]:
        try:
            cur = conn.execute(f"SELECT * FROM {t} WHERE {where}=%s", (aid,))
            print(t, "A609:", cur.fetchall())
        except Exception as e:
            print(t, "ERR", e)
            conn.rollback()
    # check which A6xx have identity rows
    for aid in ["A604","A610","A612"]:
        r = conn.execute("SELECT * FROM provision_identities WHERE provision_id=%s", (aid,)).fetchall()
        print("identities", aid, ":", r)
