from pathlib import Path

from governance_rule.execution.codex_amendment_lifecycle import (
    AmendmentLifecycleError,
    CodexAmendmentRequestLedger,
)
from governance_rule.execution.codex_repository import codex_readonly_connection

request_path = Path("main-system/runtime/state/codex-amendment-request-formal-rule-parity-lifecycle-2026-09-21.json")
with codex_readonly_connection() as connection:
    current_version = connection.execute(
        "SELECT value FROM metadata WHERE key='codex_version'"
    ).fetchone()[0]
    sequence = connection.execute(
        "SELECT sequence FROM revision_history ORDER BY sequence DESC LIMIT 1"
    ).fetchone()[0]
ledger = CodexAmendmentRequestLedger()
try:
    ledger.begin(
        request_path,
        current_version=current_version,
        expected_revision_sequence=sequence,
    )
except AmendmentLifecycleError as error:
    print("closed", error.code, error.detail)
print(ledger.load_record("codex-amendment-request-formal-rule-parity-lifecycle-2026-09-21")["state"])
