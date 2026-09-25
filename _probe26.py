import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import content_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

TARGETS = {
    "index_content_hash": "9f455bec3e2230926e9dcf192d8a27b7cb871e612963700189cec6a642251468",
    "fts_content_hash": "fce12d3e7152fde2d6aa7a91aea91bc130311412451f1d626e5f71646fac531c",
}

def test(name, h):
    print(f"{name}: {h}" + ("" if h not in TARGETS.values() else "  <== MATCH " +
          [k for k, v in TARGETS.items() if v == h][0]))

with codex_readonly_connection() as conn:
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_search_document' ORDER BY ordinal_position")]
    rows = [list(r) for r in conn.execute("SELECT * FROM codex_search_document")]
    idx = {c: i for i, c in enumerate(cols)}
    def key(*f):
        return lambda r: tuple(r[idx[x]] or "" for x in f)
    for name, rows_ in [
        ("pid", sorted(rows, key=key("provision_type", "provision_id"))),
        ("pid_only", sorted(rows, key=key("provision_id"))),
        ("module+pid", sorted(rows, key=key("module_code", "provision_type", "provision_id"))),
        ("law+pid", sorted(rows, key=key("law_code", "provision_type", "provision_id"))),
    ]:
        test(f"docs lists {name}", content_hash(rows_))
    # fts columns?
    fcols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_search_fts' ORDER BY ordinal_position")]
    print("fts cols:", fcols)
    frows = [list(r) for r in conn.execute("SELECT * FROM codex_search_fts")]
    fidx = {c: i for i, c in enumerate(fcols)}
    # guess: hash over just the content column (FTS5 stores indexed content)
    import hashlib
    content_col = None
    for cand in ("content", "search_content", "text"):
        if cand in fidx:
            content_col = fidx[cand]
    if content_col is None:
        # FTS5 virtual: probably (provision_type, provision_id, module_code, law_code, content)
        content_col = len(fcols) - 1
    test("fts content col join|", hashlib.sha256("|".join(str(r[content_col]) for r in frows).encode()).hexdigest())
    test("fts lists natural", content_hash(frows))
    # fts sorted by provision
    pcol = fidx.get("provision_id", 1)
    test("fts lists by pid", content_hash(sorted(frows, key=lambda r: str(r[pcol]))))
