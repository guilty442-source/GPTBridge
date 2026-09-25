import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for pid in ("CLOSURE_DELEGATED_A231_V1", "REGISTRY_A521_V1", "RULE_DELEGATED_A498_V1"):
        r = conn.execute("SELECT * FROM codex_search_document WHERE provision_id=%s", (pid,)).fetchone()
        print("doc:", r)
    # provision_lifecycle_status NOT NULL cols
    cur = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name='provision_lifecycle_status' ORDER BY ordinal_position")
    print("lifecycle cols:", cur.fetchall())
    # codex_internal_module_membership nullability + module codes list
    cur = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name='codex_internal_module_membership' ORDER BY ordinal_position")
    print("membership cols:", cur.fetchall())
    cur = conn.execute("SELECT DISTINCT module_code FROM codex_internal_module_membership ORDER BY 1")
    print("modules:", [r[0] for r in cur.fetchall()])
    # module dependency cols
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_internal_module_dependency' ORDER BY ordinal_position")
    print("dep cols:", cur.fetchall())
    for r in conn.execute("SELECT * FROM codex_internal_module_dependency LIMIT 3"):
        print("dep:", r)
    # surface cols nullability
    cur = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name='current_normative_surface' ORDER BY ordinal_position")
    print("surface cols:", cur.fetchall())
    # check codex_search_document for lifecycle new-article coverage - which articles lack docs?
    cur = conn.execute(
        "SELECT p.provision_id FROM provision_lifecycle_status p WHERE p.provision_type='article' "
        "AND p.provision_id NOT IN (SELECT provision_id FROM codex_search_document WHERE provision_type='article') "
        "ORDER BY 1")
    print("articles lacking docs:", cur.fetchall())
    # non-article lifecycle entries lacking docs
    cur = conn.execute(
        "SELECT p.provision_type, p.provision_id FROM provision_lifecycle_status p "
        "WHERE p.provision_type<>'article' AND (p.provision_type, p.provision_id) NOT IN "
        "(SELECT provision_type, provision_id FROM codex_search_document)")
    print("non-article lacking docs:", cur.fetchall())
    # how many surface rows point at objects not in lifecycle at all (directories/closures/schemas)
    cur = conn.execute(
        "SELECT object_type, COUNT(*) FROM current_normative_surface s "
        "WHERE (s.object_type, s.object_identity) NOT IN "
        "(SELECT provision_type, provision_id FROM provision_lifecycle_status) GROUP BY 1")
    print("surface non-lifecycle objects:", cur.fetchall())
