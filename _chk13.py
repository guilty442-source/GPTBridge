import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, "governance_rule")
from governance_rule.execution.codex_amendment_lifecycle import (
    CodexAmendmentRequestLedger, AmendmentLifecycleError,
)

sys.path.insert(0, "governance_rule/tests")
# replicate the test's _request helper minimally
import json as J
VERSION = "2026-09-25T10:10:06Z"
def make_request(path, rid, pred_version=VERSION):
    payload = {
        "artifact": "codex-amendment-request",
        "authority": "request-only",
        "schema": "codex-amendment-request/v1",
        "request_id": rid,
        "change_class": "architecture-authority",
        "required_review": "five-sovereign-audit-unanimous-pass",
        "not_executed": True,
        "requested_by": "decision-sovereign",
        "predecessor": {
            "codex_version": pred_version,
            "history_head": "a"*64,
            "revision_sequence": 1,
        },
        "changes": [{"table": "articles", "key": {"provision_id": "A1"}, "field": "rule", "proposed": "x"}],
    }
    Path(path).write_text(J.dumps(payload), encoding="utf-8")
    return Path(path)

tmp = Path(tempfile.mkdtemp())
ledger = CodexAmendmentRequestLedger(tmp / "ledger")

# holder acquires lock then its record is marked terminal BYPASSING
# transition() — simulating the external executor write path
first = make_request(tmp / "first.json", "holder-req")
rec = ledger.begin(first)
record = ledger.load_record("holder-req")
record["state"] = "executed"           # bypass transition(): no closed_at,
record_path = Path(record["record_path"])  # no release, like the live residue
record_path.write_text(J.dumps(record), encoding="utf-8")

second = make_request(tmp / "second.json", "successor-req")
rec2 = ledger.begin(second)
print("RECLAIM_OK state:", rec2.state)

# and a NON-terminal holder still blocks
third_path = make_request(tmp / "third.json", "live-holder")
rec3_record = ledger.load_record("successor-req")  # still submitted = live
fourth = make_request(tmp / "fourth.json", "blocked-req")
try:
    ledger.begin(fourth)
    print("FAIL: not blocked")
except AmendmentLifecycleError as e:
    print("BLOCK_OK:", str(e).split(":")[0])

# unattributed lock keeps blocking (fail closed)
import os
lockdir = ledger.locks_dir
for lf in lockdir.glob("*.lock"):
    lf.unlink()
badlock = lockdir / "lineage-" + "0"*32 + ".lock"
lockdir.mkdir(parents=True, exist_ok=True)
badlock.write_text(J.dumps({"schema": "x"}), encoding="utf-8")
# craft request whose lineage_key matches that lock file name
from governance_rule.execution.codex_amendment_contract import content_hash
lk = content_hash({"codex_version": VERSION, "history_head": "a"*64, "revision_sequence": 1})
badlock.unlink()
badlock = lockdir / f"lineage-{lk[:32]}.lock"
badlock.write_text(J.dumps({"schema": "x", "request_id": ""}), encoding="utf-8")
try:
    ledger.begin(make_request(tmp / "fifth.json", "attr-req"))
    print("FAIL: unattributed lock allowed")
except AmendmentLifecycleError as e:
    print("FAILCLOSED_OK:", str(e).split(":")[0])

