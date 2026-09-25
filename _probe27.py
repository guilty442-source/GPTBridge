import sys, io, hashlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import content_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

M = "CODEX_MODULE_AUDIT"
with codex_readonly_connection() as conn:
    row = conn.execute(
        "SELECT membership_hash, content_hash, dependency_hash, search_document_hash, "
        "lifecycle_hash, classification_hash, successor_resolution_hash "
        "FROM codex_internal_module_manifest WHERE module_code=%s", (M,)).fetchone()
    names = ["membership_hash","content_hash","dependency_hash","search_document_hash",
             "lifecycle_hash","classification_hash","successor_resolution_hash"]
    target = dict(zip(names, row))
    print("targets:", {k: v[:16] for k, v in target.items()})

    def lists(sql, params=()):
        return [list(r) for r in conn.execute(sql, params)]

    mem = lists("SELECT * FROM codex_internal_module_membership WHERE module_code=%s", (M,))
    print("mem rows:", len(mem), "| mem natural:", content_hash(mem)[:16], "want", target["membership_hash"][:16])
    mem_sorted = sorted(mem, key=lambda r: (str(r[0]), str(r[1])))
    print("mem sorted:", content_hash(mem_sorted)[:16])
    # membership with all rows (no filter)?
    # docs for module
    docs = lists("SELECT * FROM codex_search_document WHERE module_code=%s", (M,))
    print("docs n:", len(docs), "| docs natural:", content_hash(docs)[:16], "want content:", target["content_hash"][:16])
    docs_sorted = sorted(docs, key=lambda r: (str(r[0]), str(r[1])))
    print("docs sorted:", content_hash(docs_sorted)[:16])
    # deps
    dep = lists("SELECT * FROM codex_internal_module_dependency WHERE module_code=%s OR source_module=%s", (M, M))
    print("dep rows:", len(dep), "cols sample:", dep[:2])
    dep_cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_internal_module_dependency' ORDER BY ordinal_position")]
    print("dep cols:", dep_cols)
    # lifecycle rows of member provisions
    pids = [r[1] for r in mem]
    lif = lists(
        "SELECT * FROM provision_lifecycle_status WHERE provision_id = ANY(%s)", (pids,))
    print("lif rows:", len(lif), "| natural:", content_hash(lif)[:16], "want", target["lifecycle_hash"][:16])
    lif_sorted = sorted(lif, key=lambda r: (str(r[0]), str(r[1])))
    print("lif sorted:", content_hash(lif_sorted)[:16])
    cls = lists(
        "SELECT * FROM provision_law_classification WHERE provision_id = ANY(%s)", (pids,))
    print("cls rows:", len(cls), "| natural:", content_hash(cls)[:16], "want", target["classification_hash"][:16])
    cls_sorted = sorted(cls, key=lambda r: str(r[1]))
    print("cls sorted by pid:", content_hash(cls_sorted)[:16])
    # successor_resolution
    rr_cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='provision_reference_resolution_v2' ORDER BY ordinal_position")]
    print("refres cols:", rr_cols)
