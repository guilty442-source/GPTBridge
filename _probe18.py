import sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

def cols(conn, t):
    return [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name=%s ORDER BY ordinal_position", (t,))]

with codex_readonly_connection() as conn:
    for t in ("codex_internal_module_manifest", "epoch_seal_manifest",
              "implementation_obligations", "machine_schema_parity_evidence",
              "machine_schema_registry", "architecture_stale_reference_evidence",
              "codex_search_document", "current_normative_surface"):
        c = cols(conn, t)
        print(f"=== {t}: {c}")

    print("\n--- module manifest rows:")
    c = cols(conn, "codex_internal_module_manifest")
    for r in conn.execute("SELECT * FROM codex_internal_module_manifest"):
        print("  ", dict(zip(c, r)))

    print("\n--- epoch_seal_manifest:")
    c = cols(conn, "epoch_seal_manifest")
    for r in conn.execute("SELECT * FROM epoch_seal_manifest"):
        print("  ", dict(zip(c, r)))

    print("\n--- obligations status counts:")
    for r in conn.execute(
        "SELECT obligation_status, COUNT(*) FROM implementation_obligations GROUP BY 1 ORDER BY 1"):
        print("  ", r)

    print("\n--- machine_schema_registry count:")
    print("  ", conn.execute("SELECT COUNT(*) FROM machine_schema_registry").fetchone())
    print("--- machine_schema_parity_evidence statuses:")
    for r in conn.execute(
        "SELECT parity_status, COUNT(*) FROM machine_schema_parity_evidence GROUP BY 1"):
        print("  ", r)

    print("\n--- normative surface version_identity counts:")
    for r in conn.execute(
        "SELECT version_identity, status, COUNT(*) FROM current_normative_surface GROUP BY 1,2 ORDER BY 1"):
        print("  ", r)

    print("\n--- stale_reference_evidence sample:")
    c = cols(conn, "architecture_stale_reference_evidence")
    for i, r in enumerate(conn.execute("SELECT * FROM architecture_stale_reference_evidence")):
        if i < 8:
            print("  ", dict(zip(c, r)))
    print("  total:", conn.execute("SELECT COUNT(*) FROM architecture_stale_reference_evidence").fetchone())
