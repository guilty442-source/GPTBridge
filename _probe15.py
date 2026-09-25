import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    # law classification for the new articles
    cur = conn.execute(
        "SELECT provision_type, provision_id, tier, law_code FROM provision_law_classification "
        "WHERE provision_id IN ('A605','A606','A607','A608','A609','A610','A611','A612','A613','A614',"
        "'A615','A616','A617','A618','A619','A620','A621') ORDER BY provision_id")
    print("=== law_classification A605-A621:")
    for r in cur.fetchall():
        print("  ", r)
    # lifecycle rows for same
    cur = conn.execute(
        "SELECT provision_type, provision_id, lifecycle_state FROM provision_lifecycle_status "
        "WHERE provision_id IN ('A610','A612','A615','A616','A621') ORDER BY provision_id")
    print("=== lifecycle:")
    for r in cur.fetchall():
        print("  ", r)
    # metadata keys of interest (version axis + startup + language + closure)
    cur = conn.execute(
        "SELECT key, value FROM metadata WHERE key IN "
        "('codex_version','current_version','current_version_identity','version_identity',"
        "'active_provision_binding_version','governance_closure_current_version',"
        "'startup_complete_deadline_ms','governor_disposition_startup_deadline',"
        "'startup_deadline_conformance_state','language_governance_schema_lifecycle',"
        "'language_governance_final_results','governance_acceptance_state',"
        "'canonical_language_roles','canonical_cross_language_topology','language_policy_ts_role',"
        "'module_line_count_language_codes','current_version_epoch','revision_sequence')")
    print("=== metadata:")
    for r in cur.fetchall():
        print(f"  {r[0]} = {str(r[1])[:160]}")
    # seal_manifest + revision head
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='seal_manifest' ORDER BY ordinal_position")
    seal_cols = [r[0] for r in cur.fetchall()]
    print("=== seal_manifest cols:", seal_cols)
    cur = conn.execute("SELECT * FROM seal_manifest ORDER BY rowid DESC LIMIT 2") if False else None
    cur = conn.execute("SELECT * FROM seal_manifest")
    rows = cur.fetchall()
    for r in rows[-3:]:
        print("  seal:", dict(zip(seal_cols, r)))
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='revision_history' ORDER BY ordinal_position")
    rev_cols = [r[0] for r in cur.fetchall()]
    print("=== revision_history cols:", rev_cols)
    cur = conn.execute("SELECT * FROM revision_history")
    rows = cur.fetchall()
    print("  count:", len(rows))
    for r in rows[-2:]:
        print("  rev:", dict(zip(rev_cols, r)))
    # codex_version_epochs cols+rows
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_version_epochs' ORDER BY ordinal_position")
    ep_cols = [r[0] for r in cur.fetchall()]
    cur = conn.execute("SELECT * FROM codex_version_epochs")
    print("=== epochs:", [dict(zip(ep_cols, r)) for r in cur.fetchall()])
    # search index manifest cols+rows
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_search_index_manifest' ORDER BY ordinal_position")
    sm_cols = [r[0] for r in cur.fetchall()]
    cur = conn.execute("SELECT * FROM codex_search_index_manifest")
    print("=== search_index_manifest:", [dict(zip(sm_cols, r)) for r in cur.fetchall()])
    # module manifest cols
    for t in ("codex_module_manifest", "codex_module_directory"):
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
            "AND table_name=%s ORDER BY ordinal_position", (t,))
        c = [r[0] for r in cur.fetchall()]
        cur.execute(f"SELECT * FROM {t} LIMIT 3")
        print(f"=== {t} cols={c} n_sample={len(cur.fetchall())}")
