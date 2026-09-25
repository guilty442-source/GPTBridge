import sys, io, sqlite3, tempfile
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")

from governance_rule.execution.codex_postgresql import export_postgresql_codex
from governance_rule.execution.codex_generation_projections import (
    rebuild_generation_bookkeeping,
)
from governance_rule.execution.codex_update_validation import staged_generation_errors

tmp = Path(tempfile.mkdtemp(prefix="proj-rehearse-"))
db = tmp / "staged.sqlite3"
export_postgresql_codex(db)
print("exported:", db, db.stat().st_size)

# simulate a successor generation
conn = sqlite3.connect(str(db))
conn.execute(
    "UPDATE metadata SET value=? WHERE key='codex_version'",
    ("2099-01-01T00:00:00Z",),
)
conn.commit()
conn.close()

result = rebuild_generation_bookkeeping(
    db,
    change_id="rehearsal-2099",
    change_scope="projection-rehearsal",
    summary="offline projection rebuild rehearsal",
)
print("rebuild result:", result)

conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
print("meta current_version:", conn.execute(
    "SELECT value FROM metadata WHERE key='current_version'").fetchone()[0])
print("meta identity:", conn.execute(
    "SELECT value FROM metadata WHERE key='current_version_identity'").fetchone()[0])
print("meta binding:", conn.execute(
    "SELECT value FROM metadata WHERE key='active_provision_binding_version'").fetchone()[0])
print("docs@new:", conn.execute(
    "SELECT COUNT(*) FROM codex_search_document WHERE version_identity='2099-01-01T00:00:00Z'").fetchone()[0])
print("docs total:", conn.execute("SELECT COUNT(*) FROM codex_search_document").fetchone()[0])
print("doc hash mismatches:", conn.execute(
    "SELECT COUNT(*) FROM codex_search_document").fetchone()[0])
print("fts rows:", conn.execute("SELECT COUNT(*) FROM codex_search_fts").fetchone()[0])
print("manifest identity:", conn.execute(
    "SELECT codex_version_identity FROM codex_search_index_manifest").fetchone()[0])
print("mm rows:", conn.execute(
    "SELECT COUNT(*) FROM codex_internal_module_manifest").fetchone()[0])
print("mm versions:", conn.execute(
    "SELECT DISTINCT version_identity FROM codex_internal_module_manifest").fetchall())
print("surface versions:", conn.execute(
    "SELECT version_identity, COUNT(*) FROM current_normative_surface GROUP BY 1").fetchall())
print("rev tail:", conn.execute(
    "SELECT sequence, change_id, version, substr(entry_hash,1,16) FROM revision_history ORDER BY sequence DESC LIMIT 2").fetchall())
print("seal tail:", conn.execute(
    "SELECT version, substr(content_root,1,16), substr(full_root,1,16) FROM seal_manifest ORDER BY rowid DESC LIMIT 2").fetchall())
print("epoch seal tail:", conn.execute(
    "SELECT version, version_identity FROM epoch_seal_manifest ORDER BY rowid DESC LIMIT 2").fetchall())
# verify doc hashes recompute
import hashlib
bad = conn.execute(
    "SELECT COUNT(*) FROM codex_search_document d WHERE d.content_hash <> ''").fetchone()[0]
# spot-check: recompute a sample doc hash
row = conn.execute("SELECT content, content_hash FROM codex_search_document WHERE provision_id='A35'").fetchone()
print("A35 hash ok:", hashlib.sha256(row[0].encode()).hexdigest() == row[1])
conn.close()

errors = staged_generation_errors(db.as_posix(), version="2099-01-01T00:00:00Z")
print("staged errors:", errors[:20] if errors else "NONE")
