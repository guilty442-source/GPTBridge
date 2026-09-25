from pathlib import Path
from governance_rule.execution.codex_amendment_driver import advance_request
from governance_rule.execution.codex_amendment_lifecycle import CodexAmendmentRequestLedger
import asyncio

def fake_search(q):
    return {"ok": True, "source": "xingcheng-web-search", "query": q, "results": [{"title": "language reallocation", "url": "https://example.com", "snippet": "C/C++/C# primary"}]}

ledger = CodexAmendmentRequestLedger()
path = "main-system/runtime/state/codex-amendment-request-language-reallocation-c-cpp-csharp-20260925.json"
async def run():
    res = await advance_request(path, ledger=ledger, search=fake_search)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))

asyncio.run(run())
