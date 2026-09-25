import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as c:
    tabs = [r[0] for r in c.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY 1")]
    print([t for t in tabs if 'version' in t or 'depend' in t
           or 'package' in t or 'toolchain' in t])
    # check language-versions request content
    d = json.load(open(
        "governance_rule/execution/audit/convergence/"
        "codex-amendment-request-infra-versions-20260925.json",
        encoding="utf-8"))
    print("infra-versions request registries:",
          {ch.get("registry") for ch in d.get("proposed_successors", [])},
          {ch.get("table") for ch in d.get("changes", [])})
    for ch in d.get("changes", [])[:6]:
        print(json.dumps(ch, ensure_ascii=False)[:220])
