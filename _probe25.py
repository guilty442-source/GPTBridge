import sys, io, hashlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import content_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

TARGETS = {
    "index_content_hash": "9f455bec3e2230926e9dcf192d8a27b7cb871e612963700189cec6a642251468",
    "module_manifest_root": "84788ed1eed8d5d3cd5a3627df383b420f2ff41e0554f19bda4228ac5eb2c008",
    "fts_content_hash": "fce12d3e7152fde2d6aa7a91aea91bc130311412451f1d626e5f71646fac531c",
    "dependency_graph_hash": "da7d2847c3667a9bbfcbdbd67cf9b92a266e93280860314d75abd2d423cee7f9",
}

def test(name, h):
    tag = ""
    for k, v in TARGETS.items():
        if h == v:
            tag = f" <== MATCHES {k}"
    print(f"{name}: {h}{tag}")

with codex_readonly_connection() as conn:
    def lists(t, order=""):
        sql = f"SELECT * FROM {t}" + (f" ORDER BY {order}" if order else "")
        return [list(r) for r in conn.execute(sql)]
    docs = lists("codex_search_document")
    fts = lists("codex_search_fts")
    alias = lists("codex_search_alias")
    mm = lists("codex_internal_module_manifest")
    dep = lists("codex_internal_module_dependency")
    pol = lists("codex_search_mode_policy")
    mem = lists("codex_internal_module_membership")

    test("docs lists natural", content_hash(docs))
    test("fts lists natural", content_hash(fts))
    test("mm lists natural", content_hash(mm))
    test("dep lists natural", content_hash(dep))
    test("mem lists natural", content_hash(mem))
    test("docs+fts+alias concat", content_hash(docs + fts + alias))
    test("docs+fts concat", content_hash(docs + fts))
    test("dict of lists {doc,alias,fts}", content_hash({"codex_search_document": docs, "codex_search_alias": alias, "codex_search_fts": fts}))
    test("dict all search tables", content_hash({
        "codex_search_document": docs, "codex_search_alias": alias,
        "codex_search_fts": fts, "codex_search_mode_policy": pol}))
    test("dict doc+dep+mem", content_hash({
        "codex_search_document": docs, "dep": dep, "mem": mem}))
    # fts: maybe only some columns matter (fts virtual table has content col)
    print("fts row sample:", fts[0][:4] if fts else None)
    print("pol rows:", pol[:3])
    # dependency_graph maybe over a different dep table - try system_module_dependency
    dep2 = lists("system_module_dependency")
    test("system_module_dependency lists", content_hash(dep2))
    # index_content maybe = hash of per-doc content_hash list in doc table natural order
    test("docs content_hash col natural", content_hash([r[6] for r in docs]))
