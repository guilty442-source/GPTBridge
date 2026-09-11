import json
from pathlib import Path
root = Path(r'E:\GPTBridge\.kilo\worktrees\flossy-nerve')
for m in ['governance_rule', 'shared-layer']:
    p = root / m / 'manifest.json'
    if p.exists():
        data = json.loads(p.read_text(encoding='utf-8'))
        print(m + ': version=' + str(data.get('version')) + ', display_version=' + str(data.get('display_version')))