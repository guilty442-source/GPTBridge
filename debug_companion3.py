import sys
sys.path.insert(0, r'E:\GPTBridge')
sys.path.insert(0, r'E:\GPTBridge\shared-layer\src')
sys.path.insert(0, r'E:\GPTBridge\main-system\src-core')
sys.path.insert(0, r'E:\GPTBridge\main-system')
sys.path.insert(0, r'E:\GPTBridge\main-system\governance')
from pathlib import Path
import json

ROOT = Path(r'E:\GPTBridge')
tools_dir = ROOT / 'Standalone tools'
print('tools_dir:', tools_dir)
print('tools_dir exists:', tools_dir.exists())

for host_dir in sorted(tools_dir.iterdir(), key=lambda item: item.name.lower()):
    print('Checking:', host_dir.name)
    host_manifest_path = host_dir / 'manifest.json'
    if not host_dir.is_dir() or not host_manifest_path.is_file():
        print('  skip: not dir or no manifest')
        continue
    try:
        host_manifest = json.loads(host_manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        print('  skip: json error')
        continue
    print('  host manifest id:', host_manifest.get('id'))
    declarations = host_manifest.get('companion_tools')
    print('  companion_tools:', declarations)
    if not isinstance(declarations, list):
        print('  skip: not list')
        continue
    for declaration in declarations:
        if not isinstance(declaration, dict):
            print('  skip: not dict')
            continue
        relative_path = str(declaration.get('path') or '').strip()
        declared_id = str(declaration.get('id') or '').strip()
        print(f'  checking {declared_id} -> {relative_path}')
        if not relative_path or not declared_id:
            print('  skip: empty')
            continue
        candidate = host_dir / relative_path
        print('  candidate:', candidate)
        print('  candidate exists:', candidate.exists())
        try:
            manifest = json.loads((candidate / 'manifest.json').read_text(encoding='utf-8'))
            print('  companion manifest id:', manifest.get('id'))
        except (OSError, json.JSONDecodeError) as e:
            print('  error reading companion manifest:', e)