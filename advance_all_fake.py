from pathlib import Path
from governance_rule.execution.codex_amendment_driver import advance_request, scan_requests
from governance_rule.execution.codex_amendment_lifecycle import CodexAmendmentRequestLedger, TERMINAL_STATES
import asyncio, json

def fake_search(q):
    return {"ok": True, "source": "xingcheng-web-search", "query": q, "results": [{"title": "t", "url": "https://example.com", "snippet": "ok"}]}

async def main():
    ledger = CodexAmendmentRequestLedger()
    pending = scan_requests(ledger=ledger)
    actionable = [p for p in pending if p.get("valid") and p.get("state") not in TERMINAL_STATES]
    print(f"actionable {len(actionable)}")
    for item in actionable:
        print(f"advancing {item['request_id']}")
        res = await advance_request(item["path"], ledger=ledger, search=fake_search)
        print(f"  -> {res.get('state')} ok={res.get('ok')}")

asyncio.run(main())
