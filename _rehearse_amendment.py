import sys, io, json, sqlite3, tempfile, hashlib
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")

from governance_rule.execution.codex_postgresql import export_postgresql_codex
from governance_rule.execution.codex_successor_builder import (
    _set_candidate_version, _apply_changes, _formal_rule_errors,
)
from governance_rule.execution.codex_generation_projections import (
    rebuild_generation_bookkeeping,
)
from governance_rule.execution.codex_update_validation import staged_generation_errors

REQ = Path(r"E:\GPTBridge\governance_rule\execution\audit\convergence"
           r"\codex-amendment-request-codex-generation-convergence-20260925.json")
request = json.loads(REQ.read_text(encoding="utf-8"))
SV = "2099-06-01T00:00:00Z"

tmp = Path(tempfile.mkdtemp(prefix="amend-rehearse-"))
db = tmp / "staged.sqlite3"
export_postgresql_codex(db)
print("exported")

conn = sqlite3.connect(str(db))
_set_candidate_version(conn, SV)
applied, deferred = _apply_changes(conn, request, successor_version=SV)
conn.commit()
print("applied:", len(applied), "deferred:", len(deferred))
for d in deferred:
    print("DEFERRED:", json.dumps(d, ensure_ascii=False)[:300])
errors = list(staged_generation_errors(db.as_posix(), version=SV))
errors += _formal_rule_errors(conn)
conn.close()
print("pre-bookkeeping errors:", errors[:15] if errors else "NONE")

result = rebuild_generation_bookkeeping(
    db, change_id=request["request_id"],
    change_scope=request["change_class"], summary=request["title"])
print("bookkeeping:", result)

errors = list(staged_generation_errors(db.as_posix(), version=SV))
print("post-bookkeeping errors:", errors[:15] if errors else "NONE")

conn = sqlite3.connect(str(db))
# --- invariants ---
def q(sql, p=()):
    return conn.execute(sql, p).fetchall()
print("\n=== invariants ===")
for aid in ["A610","A611","A612","A613","A614","A615","A616","A617","A618","A619","A620","A621"]:
    print(aid,
          "art:", bool(q("SELECT 1 FROM articles WHERE provision_id=?", (aid,))),
          "| cls:", q("SELECT law_code FROM provision_law_classification WHERE provision_id=?", (aid,)),
          "| life:", q("SELECT lifecycle_state FROM provision_lifecycle_status WHERE provision_id=?", (aid,)),
          "| mem:", q("SELECT module_code FROM codex_internal_module_membership WHERE provision_id=?", (aid,)),
          "| eff:", bool(q("SELECT 1 FROM effective_provisions WHERE provision_id=?", (aid,))))
print("deadline meta:", q("SELECT value FROM metadata WHERE key='startup_complete_deadline_ms'"))
print("A199 40000 residue:", "40000" in str(q("SELECT rule||prohibition||exception FROM articles WHERE provision_id='A199'")))
print("parity:", q("SELECT status, COUNT(*) FROM machine_schema_parity_evidence GROUP BY status"))
print("parity hash-equal rows:", q(
    "SELECT COUNT(*) FROM machine_schema_parity_evidence WHERE producer_semantic_hash=validator_semantic_hash "
    "AND validator_semantic_hash=persistence_semantic_hash AND persistence_semantic_hash=canonical_semantic_hash"))
print("registry count:", q("SELECT COUNT(*) FROM machine_schema_registry"))
print("rules missing provision:", q(
    "SELECT COUNT(*) FROM formal_rule_registry f LEFT JOIN articles a ON a.provision_id=f.controlling_provision_id "
    "WHERE a.provision_id IS NULL AND f.status<>'withdrawn'"))
print("docs@", SV, ":", q("SELECT COUNT(*) FROM codex_search_document WHERE version_identity=?", (SV,)))
print("surface@", SV, ":", q("SELECT COUNT(*) FROM current_normative_surface WHERE version_identity=?", (SV,)))
print("obligation:", q("SELECT current_state FROM implementation_obligations WHERE obligation_code='OBL_MACHINE_SCHEMA_PARITY'"))
# residual '40000' anywhere in articles
bad = [r for r in conn.execute("SELECT provision_id FROM articles WHERE rule LIKE '%40000%' OR prohibition LIKE '%40000%' OR exception LIKE '%40000%'")]
print("articles still containing 40000:", bad)
conn.close()
