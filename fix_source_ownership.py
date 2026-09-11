# This script adds resilient_store.py to SHARED_LAYER_ALLOWED_SOURCES
with open(r'E:\GPTBridge\governance_rule\permission_directory\registries\permissions\source_ownership.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the line with "service_probe.py" and add resilient_store.py after it
for i, line in enumerate(lines):
    if '"service_probe.py",' in line:
        lines.insert(i + 1, '        "resilient_store.py",\n')
        break

with open(r'E:\GPTBridge\governance_rule\permission_directory\registries\permissions\source_ownership.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print('Done')