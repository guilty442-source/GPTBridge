from governance_rule.execution.codex_postgresql import authority_state
import json
state = authority_state()
# convert datetime to string
for k,v in list(state.items()):
    if hasattr(v, 'isoformat'):
        state[k] = v.isoformat()
print(json.dumps(state, indent=2, ensure_ascii=False))
