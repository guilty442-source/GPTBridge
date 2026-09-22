import sys
sys.path.insert(0, '.')
from governance_rule.execution.codex_amendment_contract import compute_seal_preview
result = compute_seal_preview('governance_rule/codex/data/governance_codex.sqlite3')
import json
print(json.dumps(result, ensure_ascii=False, indent=2))