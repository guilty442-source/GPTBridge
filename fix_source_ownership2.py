# This script adds tool_codenames.py to SHARED_LAYER_ALLOWED_SOURCES
with open(r'E:\GPTBridge\governance_rule\permission_directory\registries\permissions\source_ownership.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the line with "resilient_store.py" and add tool_codenames.py after it
for i, line in enumerate(lines):
    if '"resilient_store.py",' in line:
        lines.insert(i + 1, '        "tool_codenames.py",\n')
        break

with open(r'E:\GPTBridge\governance_rule\permission_directory\registries\permissions\source_ownership.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print('Done')