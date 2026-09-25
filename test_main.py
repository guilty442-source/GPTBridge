from pathlib import Path
from governance_rule.execution.codex_amendment_driver import advance_request
from governance_rule.execution.codex_amendment_lifecycle import CodexAmendmentRequestLedger
import asyncio, json
def fake_search(q):
    return {"ok": True, "source": "xingcheng-web-search", "query": q, "results": [{"title": "t", "url": "https://example.com", "snippet": "ok"}]}
ledger = CodexAmendmentRequestLedger()
path = "main-system/runtime/state/codex-amendment-request-main-system-csharp14-20260925.json"
async def run():
    res = await advance_request(path, ledger=ledger, search=fake_search)
    print(json.dumps(res, ensure_ascii=False, indent=2))
asyncio.run(run())
