import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_amendment_contract import search_document_hash
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for pid in ("A1", "A2", "A77"):
        d = conn.execute(
            "SELECT subject, content, content_hash FROM codex_search_document WHERE provision_id=%s", (pid,)).fetchone()
        a = conn.execute(
            "SELECT subject, rule, prohibition, exception FROM articles WHERE provision_id=%s", (pid,)).fetchone()
        if not d or not a:
            print(pid, "missing"); continue
        subj, rule, proh, exc = a
        print(f"### {pid}")
        print("  doc.content == stored:", repr(d[1][:120]))
        cands = {
            "subj\\nrule\\nproh": f"{subj}\n{rule}\n{proh}",
            "subj\\nrule\\nproh\\nexc": f"{subj}\n{rule}\n{proh}\n{exc}",
            "rule\\nproh": f"{rule}\n{proh}",
            "rule\\nproh\\nexc": f"{rule}\n{proh}\n{exc}",
            "rule+proh+exc-space": f"{rule} {proh} {exc}",
        }
        for k, v in cands.items():
            if v == d[1]:
                print("  TEMPLATE MATCH:", k)
            if search_document_hash(v) == d[2]:
                print("  HASH MATCH:", k)
        # brute: check if content == rule only
        if d[1] == rule:
            print("  content == rule")
        # check head/tail alignment
        if d[1].startswith(subj): print("  starts with subject")
        if d[1].startswith(rule): print("  starts with rule")
        if exc and d[1].endswith(exc): print("  ends with exception")
        if d[1].endswith(proh): print("  ends with prohibition")
