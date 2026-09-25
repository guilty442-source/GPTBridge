import asyncio, json
import websockets
TOKEN = "b2808b1a4aeaf8a297fae3ce261fc003655361ba16b55cdba8661a163cd2949e"
INST = "243aee46a69777a07dacf19c"
async def main():
    try:
        async with websockets.connect(f"ws://127.0.0.1:8766/?token={TOKEN}&instance={INST}",origin="null",max_size=8*1024*1024,ping_interval=20) as ws:
            await ws.send(json.dumps({"type":"command","command":"toolbox_start_tool","payload":{"tool_id":"local-model"},"request_id":"diag-start-6"}))
            for _ in range(60):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    print("TIMEOUT"); break
                d = json.loads(msg)
                s = json.dumps(d, ensure_ascii=False)
                if "toolbox" in s or "local-model" in s or "local_model" in s:
                    print("EVT:", s[:800])
                    p = d.get("payload") or {}
                    if d.get("event")=="toolbox_start_tool_result" or p.get("status") in ("completed","failed"):
                        break
    except Exception as e:
        print("WS_FAIL", type(e).__name__, str(e)[:150])
asyncio.run(main())
