import sys
sys.path.insert(0, r'E:\GPTBridge')
sys.path.insert(0, r'E:\GPTBridge\shared-layer\src')
sys.path.insert(0, r'E:\GPTBridge\main-system\src-core')
sys.path.insert(0, r'E:\GPTBridge\main-system')
sys.path.insert(0, r'E:\GPTBridge\main-system\governance')
from pathlib import Path
from tasks.toolbox_service import ToolboxService

ROOT = Path(r'E:\GPTBridge')

class GovernanceStub:
    def authorize_ai_target(self, *args, **kwargs):
        pass

service = ToolboxService(ROOT, governance=GovernanceStub())

# Check what companions are found
companions = service._declared_companion_tool_directories()
print('Companions found:', companions)
for c in companions:
    print('  ', c)
    import json
    manifest = json.loads((c / 'manifest.json').read_text(encoding='utf-8'))
    print('    id:', manifest.get('id'))
    print('    has_custom_ui:', manifest.get('has_custom_ui'))
    print('    host_tool_id:', manifest.get('host_tool_id'))
    print('    runtime_owner_tool_id:', manifest.get('runtime_owner_tool_id'))
    print('    shared_permission_owner:', manifest.get('shared_permission_owner'))
    print('    companion_tool:', manifest.get('companion_tool'))
    print('    companion_owner:', manifest.get('companion_owner'))
    owner = service._runtime_owner_tool_id(manifest.get('id'), manifest)
    print('    runtime owner:', owner)

# Check the condition in _reconnect_companion_source_uis
print('\nChecking conditions:')
for c in companions:
    manifest = json.loads((c / 'manifest.json').read_text(encoding='utf-8'))
    tool_id = str(manifest.get("id") or "").strip()
    owner = service._runtime_owner_tool_id(tool_id, manifest)
    has_custom_ui = manifest.get("has_custom_ui") is True
    print(f'  {tool_id}: owner={owner}, has_custom_ui={has_custom_ui}, owner==xingcheng: {owner == "xingcheng"}')