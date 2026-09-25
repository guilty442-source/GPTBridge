import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    print("codex_version:", c.execute(
        "SELECT value FROM metadata WHERE key='codex_version'").fetchone()[0])
    print("current_version:", c.execute(
        "SELECT value FROM metadata WHERE key='current_version'").fetchone()[0])
    print("A623:", c.execute(
        "SELECT subject FROM articles WHERE provision_id='A623'").fetchone())
    print("lifecycle:", c.execute(
        "SELECT lifecycle_state FROM provision_lifecycle_status "
        "WHERE provision_id='A623'").fetchone())
    print("classification:", c.execute(
        "SELECT tier, law_code FROM provision_law_classification "
        "WHERE provision_id='A623'").fetchone())
    print("unclassified:", c.execute(
        "SELECT COUNT(*) FROM provision_lifecycle_status ls "
        "WHERE ls.lifecycle_state='active' AND NOT EXISTS ("
        "SELECT 1 FROM provision_law_classification cl "
        "WHERE cl.provision_type=ls.provision_type "
        "AND cl.provision_id=ls.provision_id)").fetchone()[0])
    print("seal:", c.execute(
        "SELECT certification_state FROM seal_manifest "
        "ORDER BY version DESC LIMIT 1").fetchone())
