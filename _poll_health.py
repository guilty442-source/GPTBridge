import json, time, urllib.request, sys

for i in range(30):
    try:
        resp = urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5)
        body = json.loads(resp.read().decode('utf-8'))
        state = body.get('runtime_state', '?')
        gov = body.get('governance_ready', False)
        phase = body.get('phase', '?')
        dur = body.get('phase_duration_ms', 0)
        print(f'[{i*3}s] state={state} governance={gov} phase={phase} dur={dur}ms')
        if state == 'ready':
            print('BACKEND READY!')
            print(json.dumps(body, indent=2, ensure_ascii=False)[:800])
            sys.exit(0)
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode('utf-8'))
        state = body.get('runtime_state', '?')
        phase = body.get('phase', '?')
        dur = body.get('phase_duration_ms', 0)
        print(f'[{i*3}s] HTTP {e.code} state={state} phase={phase} dur={dur}ms')
        if state == 'ready':
            print('BACKEND READY!')
            print(json.dumps(body, indent=2, ensure_ascii=False)[:800])
            sys.exit(0)
    except Exception as e:
        print(f'[{i*3}s] error: {e}')
    time.sleep(3)

print('Timeout: backend did not reach ready state in 90s')
sys.exit(1)
