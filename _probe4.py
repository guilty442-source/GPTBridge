import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

def cols(conn, t):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name=%s "
        "ORDER BY ordinal_position", (t,))
    return [r[0] for r in cur.fetchall()]

with codex_readonly_connection() as conn:
    for t in ("codex_version_epochs", "epoch_seal_manifest", "codex_search_index_manifest",
              "codex_internal_module_manifest", "current_normative_surface"):
        c = cols(conn, t)
        cur = conn.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        print(f"=== {t} cols={c} rows={len(rows)}")
        for r in rows[:6]:
            print("   ", {k: str(v)[:80] for k, v in zip(c, r)})

    # machine schema parity
    c = cols(conn, "machine_schema_parity_evidence")
    cur = conn.execute("SELECT * FROM machine_schema_parity_evidence")
    rows = cur.fetchall()
    from collections import Counter
    print("=== machine_schema_parity_evidence cols:", c, "rows:", len(rows))
    if rows:
        print("   sample:", dict(zip(c, rows[0])))
        for cand in ("status", "parity_status", "state"):
            if cand in c:
                print("   ", cand, Counter(r[c.index(cand)] for r in rows))

    # obligations
    c = cols(conn, "implementation_obligations")
    cur = conn.execute("SELECT * FROM implementation_obligations")
    rows = cur.fetchall()
    print("=== implementation_obligations cols:", c, "rows:", len(rows))
    if rows:
        for cand in ("status", "obligation_status", "state"):
            if cand in c:
                print("   ", cand, Counter(r[c.index(cand)] for r in rows))
        print("   sample:", {k: str(v)[:70] for k, v in zip(c, rows[0])})

    # provision_law_classification coverage
    c = cols(conn, "provision_law_classification")
    cur = conn.execute("SELECT * FROM provision_law_classification")
    lrows = cur.fetchall()
    print("=== provision_law_classification cols:", c, "rows:", len(lrows))
    classified = {r[c.index("provision_id")] for r in lrows} if "provision_id" in c else set()
    cur = conn.execute("SELECT provision_id FROM articles")
    allp = {r[0] for r in cur.fetchall()}
    print("   unclassified provisions:", sorted(allp - classified)[:50])

    # normative identities
    for t in ("normative_registry_fact", "codex_normative_convergence_registry"):
        c = cols(conn, t)
        cur = conn.execute(f"SELECT * FROM {t}")
        rows = cur.fetchall()
        print(f"=== {t} cols={c} rows={len(rows)}")
        if rows:
            print("   sample:", {k: str(v)[:70] for k, v in zip(c, rows[0])})
