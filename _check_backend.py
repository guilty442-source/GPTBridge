import json, asyncio, websockets, pathlib, os, urllib.request

# Get instance from health endpoint
resp = urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5)
body = json.loads(resp.read().decode('utf-8'))
instance = body.get('workspace_instance_id', '')
state = body.get('runtime_state', '')
print(f'Health: state={state} instance={instance}')

token = pathlib.Path(os.environ['LOCALAPPDATA'] + '/GPTBridge/ipc/session-token').read_text('utf-8').strip().lower()
print(f'Token: {token[:16]}...')

async def check():
    uri = f'ws://127.0.0.1:8765?token={token}&instance={instance}'
    try:
        async with websockets.connect(uri, open_timeout=5) as ws:
            await ws.send(json.dumps({'command': 'app:get_status', 'payload': {}}))
            # Wait for the result event
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                data = json.loads(raw)
                event = data.get('event', '')
                if event == 'app:get_status_result':
                    payload = data.get('payload', data)
                    print(f'Status result:')
                    full = json.dumps(payload, ensure_ascii=False, indent=2)
                    print(full[:3000])
                    break
                elif event == 'COMMAND_RECEIVED':
                    print(f'Command received, waiting for result...')
                else:
                    print(f'Event: {event}')
    except Exception as e:
        print(f'WebSocket error: {e}')

asyncio.run(check())
