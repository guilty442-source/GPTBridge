import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    # tier distribution vs surface layer for articles
    print("--- law_classification tier counts:")
    for r in conn.execute("SELECT tier, COUNT(*) FROM provision_law_classification GROUP BY 1"):
        print("  ", r)
    # article surface rows: layer vs provision tier
    cur = conn.execute(
        "SELECT s.surface_layer, c.tier, COUNT(*) FROM current_normative_surface s "
        "JOIN provision_law_classification c ON c.provision_id=s.object_identity "
        "WHERE s.object_type='article' GROUP BY 1,2 ORDER BY 1,2")
    for r in cur.fetchall():
        print("  surf-layer x tier:", r)
    # articles NOT on surface
    cur = conn.execute(
        "SELECT COUNT(*) FROM provision_lifecycle_status p WHERE p.provision_type='article' "
        "AND ('article', p.provision_id) NOT IN (SELECT object_type, object_identity FROM current_normative_surface)")
    print("  articles not on surface:", cur.fetchone())
    # which article lifecycle states absent
    cur = conn.execute(
        "SELECT p.lifecycle_state, COUNT(*) FROM provision_lifecycle_status p WHERE p.provision_type='article' "
        "AND p.provision_id NOT IN (SELECT object_identity FROM current_normative_surface WHERE object_type='article') "
        "GROUP BY 1")
    for r in cur.fetchall():
        print("  absent article by lc:", r)
    # formal-rule surface coverage
    cur = conn.execute("SELECT COUNT(*) FROM current_normative_surface WHERE object_type='formal-rule'")
    print("surface formal-rule n:", cur.fetchone())
    cur = conn.execute("SELECT COUNT(*) FROM formal_rule_registry WHERE status<>'withdrawn'")
    print("registry active n:", cur.fetchone())
    cur = conn.execute(
        "SELECT object_identity FROM current_normative_surface WHERE object_type='formal-rule' "
        "AND object_identity NOT IN (SELECT rule_code FROM formal_rule_registry)")
    print("  surface rules not in registry:", cur.fetchall()[:10])
    cur = conn.execute(
        "SELECT rule_code FROM formal_rule_registry WHERE status<>'withdrawn' "
        "AND rule_code NOT IN (SELECT object_identity FROM current_normative_surface WHERE object_type='formal-rule')")
    print("  active rules not on surface:", len(cur.fetchall()))
    # machine-schema surface n vs registry
    cur = conn.execute("SELECT COUNT(*) FROM current_normative_surface WHERE object_type='machine-schema'")
    print("surface machine-schema n:", cur.fetchone())
    # 'closure' object_type sources
    for r in conn.execute("SELECT * FROM current_normative_surface WHERE object_type='closure' LIMIT 3"):
        print("  closure row:", r)
    for r in conn.execute("SELECT * FROM current_normative_surface WHERE object_type='directory' LIMIT 3"):
        print("  directory row:", r)
    # membership for new articles — pick module convention: A604 -> CODEX_MODULE_AUTHORITY; check A612/A615 module
    for r in conn.execute("SELECT * FROM codex_internal_module_membership WHERE provision_id IN ('A610','A612','A615','A616','A621','A609','A605')"):
        print("  membership:", r)
