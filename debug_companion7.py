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

# Check what companions are found - add debug
print('project_root:', service.project_root)
print('tools_dir:', service.tools_dir)

# Try to manually trace the logic
from tasks.toolbox_manifest import ManifestMixin

# Access the private method
for host_dir in sorted(service.tools_dir.iterdir(), key=lambda item: item.name.lower()):
    host_manifest_path = host_dir / "manifest.json"
    if not host_dir.is_dir() or not host_manifest_path.is_file():
        continue
    import json
    try:
        host_manifest = json.loads(host_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    print('Checking:', host_dir.name, 'id:', host_manifest.get('id'))
    declarations = host_manifest.get("companion_tools")
    print('  companion_tools:', declarations)
    if not isinstance(declarations, list):
        continue
    for declaration in declarations:
        if not isinstance(declaration, dict):
            continue
        relative_path = str(declaration.get("path") or "").strip()
        declared_id = str(declaration.get("id") or "").strip()
        print(f'  checking {declared_id} -> {relative_path}')
        if not relative_path or not declared_id:
            continue
        candidate = host_dir / relative_path
        print('  candidate:', candidate)
        try:
            validated = service._validated_tool_directory(candidate)
            print('  validated:', validated)
            manifest = json.loads((validated / "manifest.json").read_text(encoding="utf-8"))
            print('  manifest id:', manifest.get("id"))
        except Exception as e:
            print('  ERROR:', e)