import json
from pathlib import Path
root = Path(r'E:\GPTBridge\.kilo\worktrees\flossy-nerve')
for m in ['ai-assistant', 'global-cleaner', 'investment-mobile', 'system-rescue']:
    manifest = json.loads((root / m / 'manifest.json').read_text(encoding='utf-8'))
    print(m + ': id=' + str(manifest.get('id')) + ', host_tool_id=' + str(manifest.get('host_tool_id')) + ', runtime_owner=' + str(manifest.get('runtime_owner_tool_id')))