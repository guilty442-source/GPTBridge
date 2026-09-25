import sys, io, hashlib
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

def test(name, h):
    tag = ""
    for k, v in TARGETS.items():
        if h == v:
            tag = f" <== MATCHES {k}"
    print(f"{name}: {h}{tag}")

with codex_readonly_connection() as conn:
    docs = conn.execute("SELECT provision_type, provision_id, content_hash FROM codex_search_document").fetchall()
    hashes = [r[2] for r in docs]
    keys = [(r[0], r[1]) for r in docs]
    test("join| doc order", hashlib.sha256("|".join(hashes).encode()).hexdigest())
    test("join| pid order", hashlib.sha256("|".join(h for *_ , h in sorted(zip(keys, hashes))).encode()).hexdigest())
    test("concat doc order", hashlib.sha256("".join(hashes).encode()).hexdigest())
    test("concat pid order", hashlib.sha256("".join(h for *_, h in sorted(zip(keys, hashes))).encode()).hexdigest())
    test("content_hash of hash-list", content_hash(hashes))
    test("content_hash sorted hash-list", content_hash(sorted(hashes)))
    # alias hash variants
    alias_cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_search_alias' ORDER BY ordinal_position")]
    print("alias cols:", alias_cols)
    alias_rows = conn.execute("SELECT * FROM codex_search_alias").fetchall()
    test("alias join| first col", hashlib.sha256("|".join(str(r[0]) for r in alias_rows).encode()).hexdigest())
    test("alias content_hash rows-of-lists", content_hash([list(r) for r in alias_rows]))
