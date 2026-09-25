import sys, io, sqlite3, tempfile, glob, os, hashlib
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_postgresql import export_postgresql_codex

tmp = Path(tempfile.mkdtemp(prefix="proj-diff2-"))
old_db = tmp / "old.sqlite3"
export_postgresql_codex(old_db)
new_db = Path(glob.glob(os.path.join(tempfile.gettempdir(), "proj-rehearse-*", "staged.sqlite3"))[-1])

a = sqlite3.connect(str(old_db)); b = sqlite3.connect(str(new_db))
old_docs = {f"{r[0]}:{r[1]}": (r[2], r[3]) for r in a.execute(
    "SELECT provision_type, provision_id, content, content_hash FROM codex_search_document")}
new_docs = {f"{r[0]}:{r[1]}": (r[2], r[3]) for r in b.execute(
    "SELECT provision_type, provision_id, content, content_hash FROM codex_search_document")}
diff = [k for k in new_docs if new_docs[k][1] != old_docs[k][1]]

# For each diff, check whether NEW content equals what the governing table
# currently says (i.e. doc was stale) vs a template bug.
arts = {str(r[0]): (str(r[1]), str(r[2]), str(r[3]), str(r[4])) for r in a.execute(
    "SELECT provision_id, subject, rule, prohibition, exception FROM articles")}
prin = {str(r[0]): (str(r[1]), str(r[2])) for r in a.execute(
    "SELECT provision_id, statement, binding FROM principles")}
edicts = {str(r[0]): (str(r[1]), str(r[2]), str(r[3])) for r in a.execute(
    "SELECT provision_id, area, edict, immutability FROM edicts")}
sovs = {str(r[0]): (str(r[1]), str(r[2]), str(r[3])) for r in a.execute(
    "SELECT sovereign_id, area, rank, basis FROM sovereigns")}
stale = template_bug = 0
bugs = []
for k in diff:
    ptype, pid = k.split(":", 1)
    new_content = new_docs[k][0]
    if ptype == "article" and pid in arts:
        s, r, p, e = arts[pid]
        expect = f"{s}\n{r}\n{p}\n{e}"
    elif ptype == "principle" and pid in prin:
        expect = f"{prin[pid][0]} {prin[pid][1]}"
    elif ptype == "edict" and pid in edicts:
        expect = f"{edicts[pid][0]} {edicts[pid][1]} {edicts[pid][2]}"
    elif ptype == "sovereign" and pid in sovs:
        expect = f"{pid} {sovs[pid][0]} {sovs[pid][1]} {sovs[pid][2]}"
    else:
        expect = f"{ptype} {pid} resolves normative detail through its registered owner"
    if new_content == expect:
        stale += 1
    else:
        template_bug += 1
        bugs.append(k)
print("stale-doc fixes:", stale, "| template bugs:", template_bug)
for k in bugs[:10]:
    print("BUG:", k, repr(new_docs[k][0][:80]), "| old:", repr(old_docs[k][0][:80]))
