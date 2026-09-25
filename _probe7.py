import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    # normative identities vs law classification — all provision types
    cur = conn.execute("SELECT provision_type, provision_id FROM provision_identities")
    idents = set(cur.fetchall())
    cur = conn.execute("SELECT provision_type, provision_id FROM provision_law_classification")
    classified = set(cur.fetchall())
    missing = idents - classified
    from collections import Counter
    print("identity types:", Counter(t for t, _ in idents))
    print("classified types:", Counter(t for t, _ in classified))
    print("missing count:", len(missing))
    print("missing sample:", sorted(missing)[:40])

    # active non-article normative identities missing classification
    cur = conn.execute("SELECT provision_type, provision_id, lifecycle_state FROM provision_lifecycle_status")
    lc = cur.fetchall()
    active = {(t, p) for t, p, s in lc if s == "active"}
    miss_active = missing & active
    print("missing+active:", len(miss_active), sorted(miss_active)[:40])

    # FREEZE_PENDING
    cur = conn.execute("SELECT key, value FROM metadata")
    for k, v in cur.fetchall():
        vs = str(v)
        if "FREEZE" in vs or "freeze" in vs.lower() or "language" in k.lower() or "closure" in k.lower():
            print("meta:", k, "=", vs[:140])
