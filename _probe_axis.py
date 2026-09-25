import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as connection:
    classifications = list(connection.execute(
        "SELECT provision_type, provision_id, law_code, tier "
        "FROM provision_law_classification"))
    classified = {(str(p), str(i)) for p, i, _l, _t in classifications}
    expected = set()
    for table, ptype, col in (
        ("principles", "principle", "provision_id"),
        ("articles", "article", "provision_id"),
        ("edicts", "edict", "provision_id"),
        ("sovereigns", "sovereign", "sovereign_id"),
    ):
        expected |= {(ptype, str(r[0])) for r in connection.execute(
            f"SELECT {col} FROM {table}")}
    delegated = {(str(r[0]), str(r[1])) for r in connection.execute(
        "SELECT provision_type, provision_id FROM provision_lifecycle_status "
        "WHERE lifecycle_state='active' AND provision_type IN "
        "('closure-definition','formal-rule','registry-rule')")}
    print("delegated active:", len(delegated))
    expected |= delegated
    print("len(classified):", len(classified), "len(rows):", len(classifications))
    print("classified==expected:", classified == expected)
    print("extra:", sorted(classified - expected)[:10])
    print("missing:", sorted(expected - classified)[:10])
