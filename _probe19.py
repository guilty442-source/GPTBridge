import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    print("--- obligations current_state counts:")
    for r in conn.execute(
        "SELECT current_state, COUNT(*) FROM implementation_obligations GROUP BY 1 ORDER BY 1"):
        print("  ", r)
    print("--- obligations target_state counts:")
    for r in conn.execute(
        "SELECT target_state, COUNT(*) FROM implementation_obligations GROUP BY 1 ORDER BY 1"):
        print("  ", r)
    print("--- obligations sample (74-claim one):")
    for r in conn.execute(
        "SELECT obligation_code, target_state, current_state, declaration_provision FROM implementation_obligations "
        "WHERE obligation_code LIKE '%SCHEMA%' OR obligation_code LIKE '%PARITY%' LIMIT 10"):
        print("  ", r)
    print("\n--- machine_schema_registry count:", conn.execute("SELECT COUNT(*) FROM machine_schema_registry").fetchone())
    print("--- parity evidence statuses:")
    for r in conn.execute(
        "SELECT status, COUNT(*) FROM machine_schema_parity_evidence GROUP BY 1"):
        print("  ", r)
    print("--- parity sample:")
    for i, r in enumerate(conn.execute("SELECT schema_code, status, reason, validated_against_version FROM machine_schema_parity_evidence LIMIT 5")):
        print("  ", r)
    print("\n--- normative surface version_identity counts:")
    for r in conn.execute(
        "SELECT version_identity, status, COUNT(*) FROM current_normative_surface GROUP BY 1,2 ORDER BY 1"):
        print("  ", r)
    print("\n--- stale_reference_evidence rows:")
    c = [x[0] for x in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='architecture_stale_reference_evidence' ORDER BY ordinal_position")]
    for i, r in enumerate(conn.execute("SELECT * FROM architecture_stale_reference_evidence")):
        print("  ", dict(zip(c, r)))
    print("\n--- superseded refs: active provisions referencing superseded:")
    cur = conn.execute(
        "SELECT COUNT(DISTINCT p.provision_id) FROM articles p "
        "JOIN provision_reference_resolution_v2 r ON r.referencing_provision = p.provision_id "
        "WHERE r.referenced_lifecycle = 'superseded'")
    try:
        print("  count:", cur.fetchone())
    except Exception as e:
        print("  err:", e)
