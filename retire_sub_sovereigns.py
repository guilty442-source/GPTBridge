import sqlite3
from governance_rule.execution.convergence.successor_framework import apply_sub_sovereign_retirement

# The 13 sub-sovereigns from architecture_registry.json (converting underscores to hyphens to match database)
identities = [
    'change-acceptance-sub-sovereign',
    'channel-contract-sync-sub-sovereign',
    'cleanup-retention-sync-sub-sovereign',
    'data-governance-sub-sovereign',
    'dependency-sync-sub-sovereign',
    'language-review-sub-sovereign',
    'policy-architecture-sub-sovereign',
    'release-update-sync-sub-sovereign',
    'repair-backup-sync-sub-sovereign',
    'runtime-state-sync-sub-sovereign',
    'startup-sub-sovereign',
    'system-sub-sovereign',
    'identity-group-sub-sovereign',  # xingcheng-assistant-identity-group in architecture registry
]

conn = sqlite3.connect('governance_rule/codex/data/governance_codex.sqlite3')

print('Retiring sub-sovereigns...')
result = apply_sub_sovereign_retirement(conn, identities, version='2026-09-21T12:00:00Z')
print(result.as_dict())
conn.close()