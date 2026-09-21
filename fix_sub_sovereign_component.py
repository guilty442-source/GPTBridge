import json

with open('governance_rule/execution/audit/architecture_registry.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# Fix the sub-sovereign-orchestration component - change its architectural_role
for comp in data['components']:
    if comp['component_id'] == 'sub-sovereign-orchestration':
        comp['architectural_role'] = 'development-maintenance'
        # Also remove owner_sub_sovereign if it still exists (should already be removed)
        if 'owner_sub_sovereign' in comp:
            del comp['owner_sub_sovereign']
        print(f"Fixed component: {comp['component_id']}")

with open('governance_rule/execution/audit/architecture_registry.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print('Fixed sub-sovereign-orchestration component')