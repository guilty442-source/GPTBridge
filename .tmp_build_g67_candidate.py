from datetime import datetime, timezone
from pathlib import Path

from governance_rule.execution.codex_amendment_lifecycle import CodexAmendmentRequestLedger
from governance_rule.execution.codex_repository import codex_readonly_connection
from governance_rule.execution.codex_successor_builder import build_successor

request_path = Path("main-system/runtime/state/codex-amendment-request-formal-rule-parity-lifecycle-2026-09-21.json")
source = Path("governance_rule/codex/data/governance_codex.sqlite3")
output = Path("main-system/runtime/state/codex-candidates/codex-candidate-formal-rule-parity-2026-09-21.sqlite3")

with codex_readonly_connection() as connection:
    current_version = connection.execute(
        "SELECT value FROM metadata WHERE key='codex_version'"
    ).fetchone()[0]
    sequence = connection.execute(
        "SELECT sequence FROM revision_history ORDER BY sequence DESC LIMIT 1"
    ).fetchone()[0]

successor_version = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
result = build_successor(
    request_path,
    source,
    output,
    ledger=CodexAmendmentRequestLedger(),
    successor_version=successor_version,
    expected_current_version=current_version,
    expected_revision_sequence=sequence,
)
print(result.as_dict())
