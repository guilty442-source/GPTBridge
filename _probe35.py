import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    print("--- provision_lifecycle_status by type/state:")
    for r in conn.execute(
        "SELECT provision_type, lifecycle_state, COUNT(*) FROM provision_lifecycle_status "
        "GROUP BY 1,2 ORDER BY 1,2"):
        print("  ", r)
    print("--- normative_registry_fact:")
    c = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='normative_registry_fact' ORDER BY ordinal_position")]
    print("  cols:", c)
    print("  n:", conn.execute("SELECT COUNT(*) FROM normative_registry_fact").fetchone())
    for r in conn.execute("SELECT * FROM normative_registry_fact LIMIT 3"):
        print("  ", r)
    # closure registry columns + sample
    for r in conn.execute("SELECT * FROM closure_definition_registry LIMIT 2"):
        print("  closure:", r)
    # effective_provisions role?
    c = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='effective_provisions' ORDER BY ordinal_position")]
    print("effective_provisions cols:", c)
    # surface object_type/layer counts
    for r in conn.execute(
        "SELECT surface_layer, object_type, COUNT(*) FROM current_normative_surface GROUP BY 1,2 ORDER BY 1,2"):
        print("  surface:", r)
