import json
from pathlib import Path

src = Path(
    r"E:\GPTBridge\governance_rule\execution\audit\convergence"
    r"\codex-amendment-request-cfamily-primary-stack-one-format-20260925-r5.json"
)
dst = src.with_name(
    "codex-amendment-request-cfamily-primary-stack-one-format-20260925-r6.json"
)

d = json.loads(src.read_text(encoding="utf-8"))
d["request_id"] = "cfamily-primary-stack-one-format-20260925-r6"
d["origin"] = (
    "human-governor directive 2026-09-25: unify file formats "
    "(one language one format), optimize for high performance, high "
    "execution speed, low consumption and low resource footprint, reduce "
    "Python dependency, C/C++/C# primary on .NET 10; r6 retry after r5 "
    "audit xingcheng search exceeded the 30s flow deadline "
    "(AUDIT_FLOW_DEADLINE_EXCEEDED) — content unchanged"
)
d.setdefault("verification", {})["requested_at"] = "2026-09-25T09:10:00Z"

dst.write_text(
    json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("wrote", dst)

import sys
sys.path[:0] = [r"E:\GPTBridge\governance_rule", r"E:\GPTBridge\shared-layer\src"]
from governance_rule.execution.codex_amendment_lifecycle import (
    load_amendment_request,
)
r = load_amendment_request(dst)
print("request_id:", r.request_id, "| hash:", r.request_hash[:16])
