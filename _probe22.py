import sys, io, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import content_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

TARGET_INDEX = "9f455bec3e2230926e9dcf192d8a27b7cb871e612963700189cec6a642251468"

with codex_readonly_connection() as conn:
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='codex_search_document' ORDER BY ordinal_position")]
    rows = conn.execute("SELECT * FROM codex_search_document").fetchall()
    print("cols:", cols, "rows:", len(rows))

    dicts = [dict(zip(cols, r)) for r in rows]

    # candidate orderings
    keys = {
        "by_pid": sorted(dicts, key=lambda d: (d["provision_type"], d["provision_id"])),
        "by_pid_only": sorted(dicts, key=lambda d: d["provision_id"]),
        "as_select": dicts,
        "by_module": sorted(dicts, key=lambda d: (d["module_code"] or "", d["provision_type"], d["provision_id"])),
        "by_law": sorted(dicts, key=lambda d: (d["law_code"] or "", d["provision_type"], d["provision_id"])),
    }
    for name, ordered in keys.items():
        h = content_hash(ordered)
        print(f"{name}: {h} {'<== MATCH' if h == TARGET_INDEX else ''}")
    # row-list variant
    for name, ordered in keys.items():
        h = content_hash([[d[c] for c in cols] for d in ordered])
        print(f"{name} (listrows): {h} {'<== MATCH' if h == TARGET_INDEX else ''}")
    # content-hash list variant: hash of sorted doc content_hashes
    h = content_hash(sorted(d["content_hash"] for d in dicts))
    print("sorted content_hashes:", h, "MATCH" if h == TARGET_INDEX else "")
    # fingerprints dict {pid: content_hash}
    h = content_hash({f"{d['provision_type']}:{d['provision_id']}": d["content_hash"] for d in dicts})
    print("dict pid->hash:", h, "MATCH" if h == TARGET_INDEX else "")
