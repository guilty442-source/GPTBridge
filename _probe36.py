import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import search_document_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    # A610 membership / law class
    for r in conn.execute("SELECT * FROM codex_internal_module_membership WHERE provision_id IN ('A604','A610','A612','A615','A616','A621')"):
        print("membership:", r)
    # sovereign doc template check on unchanged sovereign
    s = conn.execute("SELECT * FROM sovereigns WHERE sovereign_id='decision-sovereign'").fetchone()
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='decision-sovereign'").fetchone()
    print("sovereign row:", s)
    print("doc:", repr(d[0]))
    pid, name, area, rank, basis = s[1], s[2], s[3], s[4], s[5]
    cands = {
        "id name rank basis": f"{pid} {name} {rank} {basis}",
        "id name area basis": f"{pid} {name} {area} {basis}",
        "id name area rank basis": f"{pid} {name} {area} {rank} {basis}",
        "id name rank": f"{pid} {name} {rank}",
        "id name": f"{pid} {name}",
    }
    for k, v in cands.items():
        if search_document_hash(v) == d[1]:
            print("  SOV TEMPLATE:", k)
    # principle/edict template re-verify
    p = conn.execute("SELECT * FROM principles WHERE provision_id='P1'").fetchone()
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='P1'").fetchone()
    print("P1:", p, "\ndoc:", repr(d[0]))
    for k, v in {"stmt bind": f"{p[2]} {p[3]}", "stmt": f"{p[2]}", "pid stmt bind": f"{p[1]} {p[2]} {p[3]}"}.items():
        if search_document_hash(v) == d[1]:
            print("  PRIN TEMPLATE:", k)
    e = conn.execute("SELECT * FROM edicts WHERE provision_id='E2'").fetchone()
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='E2'").fetchone()
    print("E2:", e, "\ndoc:", repr(d[0]))
    for k, v in {"area edict immut": f"{e[2]} {e[3]} {e[4]}", "edict": f"{e[3]}", "area edict": f"{e[2]} {e[3]}"}.items():
        if search_document_hash(v) == d[1]:
            print("  EDICT TEMPLATE:", k)
    # lifecycle row template (use an existing article)
    for r in conn.execute("SELECT * FROM provision_lifecycle_status WHERE provision_id='A604'"):
        print("lifecycle A604:", r)
    # closure_definition_registry rows need docs - check a surface entry for a registry-rule
    for r in conn.execute("SELECT * FROM current_normative_surface WHERE object_type='formal-rule' LIMIT 3"):
        print("surface formal-rule:", r)
    for r in conn.execute("SELECT * FROM current_normative_surface WHERE object_identity='A610' OR object_identity='A612'"):
        print("surface A610/A612:", r)
    # synthetic doc content_hash verify
    d = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='CLOSURE_DELEGATED_A231_V1'").fetchone()
    print("closure doc hash match:", search_document_hash(d[0]) == d[1])
