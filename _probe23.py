import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import content_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

TARGETS = {
    "index_content_hash": "9f455bec3e2230926e9dcf192d8a27b7cb871e612963700189cec6a642251468",
    "module_manifest_root": "84788ed1eed8d5d3cd5a3627df383b420f2ff41e0554f19bda4228ac5eb2c008",
    "alias_hash": "7364dd661303356510416740a682dd6d71d0b5e8f9c9397c49517c02b561e9cf",
    "fts_content_hash": "fce12d3e7152fde2d6aa7a91aea91bc130311412451f1d626e5f71646fac531c",
    "dependency_graph_hash": "da7d2847c3667a9bbfcbdbd67cf9b92a266e93280860314d75abd2d423cee7f9",
}

def rows_of(conn, t, order=None):
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name=%s ORDER BY ordinal_position", (t,))]
    sql = f"SELECT * FROM {t}" + (f" ORDER BY {order}" if order else "")
    return [dict(zip(cols, r)) for r in conn.execute(sql)]

with codex_readonly_connection() as conn:
    docs = rows_of(conn, "codex_search_document")
    alias = rows_of(conn, "codex_search_alias")
    try:
        fts = rows_of(conn, "codex_search_fts")
    except Exception:
        fts = []
    print("docs", len(docs), "alias", len(alias), "fts", len(fts))

    def test(name, h):
        tag = ""
        for k, v in TARGETS.items():
            if h == v:
                tag = f" <== MATCHES {k}"
        print(f"{name}: {h}{tag}")

    test("alias rows", content_hash(alias))
    test("fts rows", content_hash(fts))
    test("docs+alias", content_hash({"codex_search_document": docs, "codex_search_alias": alias}))
    test("docs+fts", content_hash({"codex_search_document": docs, "codex_search_fts": fts}))
    test("docs+alias+fts", content_hash({"doc": docs, "alias": alias, "fts": fts}))
    # module manifest root over module manifest rows
    mm = rows_of(conn, "codex_internal_module_manifest")
    test("module_manifest rows", content_hash(mm))
    test("module_manifest sorted", content_hash(sorted(mm, key=lambda d: d["module_code"])))
    # module membership for root?
    mem = rows_of(conn, "codex_internal_module_membership")
    test("membership rows", content_hash(mem))
    test("membership sorted", content_hash(sorted(mem, key=lambda d: (d["provision_type"], d["provision_id"], d["module_code"]))))
    # dependency graph hash over module dependency table
    dep = rows_of(conn, "codex_internal_module_dependency")
    test("module dep rows", content_hash(dep))
    test("module dep sorted", content_hash(sorted(dep, key=lambda d: tuple(sorted(d.items())))))
    # per-doc recipe check: content_hash column == search_document_hash(content)?
    from governance_rule.execution.codex_amendment_contract import search_document_hash
    d0 = docs[0]
    print("doc0 recipe check:", search_document_hash(d0["content"]) == d0["content_hash"], "| stored:", d0["content_hash"][:16])
