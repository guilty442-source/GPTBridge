import sys, io, hashlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import search_document_hash, content_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    # one doc + its source provision
    r = conn.execute(
        "SELECT provision_type, provision_id, module_code, law_code, subject, content, content_hash, "
        "lifecycle_state, version_identity FROM codex_search_document WHERE provision_id='A35'").fetchone()
    print("doc A35:", r[:5], "hash:", r[6][:16], "lc:", r[7], "ver:", r[8])
    a = conn.execute(
        "SELECT provision_id, section_index, subject, rule, prohibition, exception "
        "FROM articles WHERE provision_id='A35'").fetchone()
    print("article fields:", [str(x)[:60] for x in a])
    content = r[5]
    print("content preview:", content[:300])
    print("content tail:", content[-200:])
    print("content len:", len(content))
    # try candidate templates
    pid, sec, subj, rule, proh, exc = a
    cands = {
        "rule": rule,
        "subject+rule": f"{subj}\n{rule}",
        "rule+proh+exc": f"{rule}\n{proh}\n{exc}",
        "pid+rule": f"{pid}\n{rule}",
        "all": f"{pid}\n{sec}\n{subj}\n{rule}\n{proh}\n{exc}",
        "all-sp": f"{pid} {sec} {subj} {rule} {proh} {exc}",
    }
    for k, v in cands.items():
        print(f"{k}: match={v == content}, hashmatch={search_document_hash(v) == r[6]}")
