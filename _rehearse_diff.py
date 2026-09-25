import sys, io, sqlite3, tempfile, glob, os
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_postgresql import export_postgresql_codex

# fresh export = "old" projection state
tmp = Path(tempfile.mkdtemp(prefix="proj-diff-"))
old_db = tmp / "old.sqlite3"
export_postgresql_codex(old_db)
new_db = Path(glob.glob(os.path.join(tempfile.gettempdir(), "proj-rehearse-*", "staged.sqlite3"))[-1])

a = sqlite3.connect(str(old_db)); b = sqlite3.connect(str(new_db))
old_docs = {f"{r[0]}:{r[1]}": (r[2], r[3]) for r in a.execute(
    "SELECT provision_type, provision_id, content, content_hash FROM codex_search_document")}
new_docs = {f"{r[0]}:{r[1]}": (r[2], r[3]) for r in b.execute(
    "SELECT provision_type, provision_id, content, content_hash FROM codex_search_document")}
diff = [k for k in new_docs if k in old_docs and new_docs[k][1] != old_docs[k][1]]
missing = [k for k in new_docs if k not in old_docs]
dropped = [k for k in old_docs if k not in new_docs]
print("content/hash diffs:", len(diff), diff[:20])
print("added:", len(missing), missing[:10])
print("dropped:", len(dropped), dropped[:10])
# module manifest row count vs old
print("mm old/new:", a.execute("SELECT COUNT(*) FROM codex_internal_module_manifest").fetchone(),
      b.execute("SELECT COUNT(*) FROM codex_internal_module_manifest").fetchone())
# surface old/new counts
print("surface old/new:", a.execute("SELECT COUNT(*) FROM current_normative_surface").fetchone(),
      b.execute("SELECT COUNT(*) FROM current_normative_surface").fetchone())
# seal manifest rows
print("seal old/new:", a.execute("SELECT COUNT(*) FROM seal_manifest").fetchone(),
      b.execute("SELECT COUNT(*) FROM seal_manifest").fetchone())
