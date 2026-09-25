import sqlite3
c = sqlite3.connect(r"file:E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3?mode=ro&immutable=1", uri=True)
print("sqlite codex_version:", c.execute("select value from metadata where key='codex_version'").fetchone())
print("seal head:", c.execute("select version from seal_manifest order by version_epoch desc, version desc limit 1").fetchone())
print("rev head:", c.execute("select sequence, version from revision_history order by sequence desc limit 1").fetchone())
print("search manifest:", c.execute("select codex_version_identity, built_at_utc from codex_search_index_manifest").fetchall())
print("module manifest ver:", c.execute("select distinct version_identity from codex_internal_module_manifest").fetchall())
print("surface ver:", c.execute("select distinct version_identity, status from current_normative_surface").fetchall())

# Try reproducing seal roots on this sqlite via the canonical toolchain
import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import compute_seal_preview
try:
    p = compute_seal_preview(r"E:\GPTBridge\governance_rule\codex\data\governance_codex.sqlite3")
    print("\ncomputed seal preview:")
    for k, v in p.items():
        if isinstance(v, dict):
            print(" ", k, "-> {tables:%d}" % len(v))
        else:
            print(" ", k, "=", v)
except Exception as e:
    print("seal preview err:", e)
print("\nstored seal row:")
print(c.execute("select * from seal_manifest order by version_epoch desc, version desc limit 1").fetchone())
