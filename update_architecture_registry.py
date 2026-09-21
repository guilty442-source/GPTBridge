import json

with open('governance_rule/execution/audit/architecture_registry.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# Remove owner_sub_sovereign from all components
for comp in data['components']:
    if 'owner_sub_sovereign' in comp:
        del comp['owner_sub_sovereign']

# Update the updated_at timestamp
data['updated_at'] = '2026-09-21'

# Remove "sub-sovereign-orchestration" from architectural_roles since sub-sovereigns are retired
if 'sub-sovereign-orchestration' in data['architectural_roles']:
    data['architectural_roles'].remove('sub-sovereign-orchestration')

# Update execution_model to remove sub_sovereign reference
if 'execution_model' in data:
    data['execution_model']['sub_sovereign'] = 'retired-A592'

with open('governance_rule/execution/audit/architecture_registry.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print('Updated architecture_registry.json')
print(f"Removed owner_sub_sovereign from {len(data['components'])} components")
print(f"Architectural roles: {data['architectural_roles']}")