import json
from pathlib import Path

src = Path(
    r"E:\GPTBridge\governance_rule\execution\audit\convergence"
    r"\codex-amendment-request-cfamily-primary-stack-one-format-20260925-r4.json"
)
dst = src.with_name(
    "codex-amendment-request-cfamily-primary-stack-one-format-20260925-r5.json"
)

d = json.loads(src.read_text(encoding="utf-8"))
d["request_id"] = "cfamily-primary-stack-one-format-20260925-r5"
d["predecessor"] = {
    "codex_version": "2026-09-25T08:10:00Z",
    "version_identity": "E2:2026-09-23T03:13:43Z",
    "version_epoch": 2,
    "history_head": (
        "bedd83c81bcc3dc3d2171bac92b6ace4b9acfa8b65e56d5ab7e6e2ac94717590"
    ),
    "revision_sequence": 75,
}
d["origin"] = (
    "human-governor directive 2026-09-25: unify file formats "
    "(one language one format), optimize for high performance, high "
    "execution speed, low consumption and low resource footprint, reduce "
    "Python dependency, C/C++/C# primary on .NET 10; r5 adds NOT NULL "
    "command/fault/manual/test-flow code columns to the "
    "project_architecture_directory insert after r4 build rejection"
)
d.setdefault("verification", {})["requested_at"] = "2026-09-25T09:00:00Z"

# Fix the project_architecture_directory row: fill every NOT NULL column.
for succ in d.get("proposed_successors", []):
    if succ.get("registry") == "project_architecture_directory":
        for row in succ.get("rows", []):
            row.setdefault("command_codes", "")
            row.setdefault("fault_codes", "")
            row.setdefault("manual_codes", "")
            row.setdefault("test_flow_codes", "")

dst.write_text(
    json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("wrote", dst, dst.stat().st_size)

import sys
sys.path[:0] = [r"E:\GPTBridge\governance_rule", r"E:\GPTBridge\shared-layer\src"]
from governance_rule.execution.codex_amendment_lifecycle import (
    load_amendment_request,
)
r = load_amendment_request(dst)
print("request_id:", r.request_id, "| hash:", r.request_hash[:16])
