import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection
with codex_readonly_connection() as conn:
    for aid in ["A609", "A610", "A612", "A615"]:
        print(aid, "lineage:", conn.execute(
            "SELECT * FROM provision_lineage WHERE provision_id=%s", (aid,)).fetchall())
        print(aid, "effective:", conn.execute(
            "SELECT * FROM effective_provisions WHERE provision_id=%s", (aid,)).fetchall())
        print(aid, "lifecycle:", conn.execute(
            "SELECT * FROM provision_lifecycle_status WHERE provision_id=%s", (aid,)).fetchall())
        print()
    # how many articles lack lifecycle/effective rows overall
    print("articles w/o lifecycle:", conn.execute(
        "SELECT COUNT(*) FROM articles a LEFT JOIN provision_lifecycle_status l "
        "ON l.provision_id=a.provision_id WHERE l.provision_id IS NULL").fetchone())
    print("articles w/o effective:", conn.execute(
        "SELECT COUNT(*) FROM articles a LEFT JOIN effective_provisions e "
        "ON e.provision_id=a.provision_id WHERE e.provision_id IS NULL").fetchone())
    print("articles w/o classification:", conn.execute(
        "SELECT COUNT(*) FROM articles a LEFT JOIN provision_law_classification c "
        "ON c.provision_id=a.provision_id WHERE c.provision_id IS NULL").fetchone())
    print("articles w/o membership:", conn.execute(
        "SELECT COUNT(*) FROM articles a LEFT JOIN codex_internal_module_membership m "
        "ON m.provision_id=a.provision_id WHERE m.provision_id IS NULL").fetchone())
