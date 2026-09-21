"""Formal-rule evaluator parity run (G67).

Executes every registered evaluator through the actual runtime registry and
records each outcome's evidence identifier.  The report is the parity
artifact an amendment cites when a rule transitions
``declared-pending-evaluator-parity`` -> ``evaluator-parity-verified``.

Fail-closed semantics are inherited from :mod:`governance_rule.execution
.formal_rules`: a missing evaluator, an unreadable codex, or a failed
predicate is recorded as ``passed=False`` and is never promotable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from governance_rule.execution.formal_rules import (
    evaluate_all,
    load_formal_rules,
)

REPORT_VERSION = "formal-rule-parity-run/v1"


def _database_fingerprint(database: Path) -> str:
    return hashlib.sha256(database.read_bytes()).hexdigest()


def run_parity_evaluation(database: Path) -> dict[str, Any]:
    """Evaluate every active formal rule and return a parity report."""
    rules = load_formal_rules(database)
    outcomes = evaluate_all(rules, database=database)
    codex_version = ""
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='codex_version'"
        ).fetchone()
        codex_version = str(row[0]) if row else ""
        connection.close()
    except sqlite3.Error:
        codex_version = ""
    entries = [
        {
            "rule_code": outcome.rule_code,
            "passed": outcome.passed,
            "decision": outcome.decision,
            "evidence_id": outcome.evidence_id,
            "reason": outcome.reason,
        }
        for outcome in outcomes
    ]
    return {
        "report": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "database": str(database),
        "database_sha256": _database_fingerprint(database),
        "codex_version": codex_version,
        "evaluated": len(entries),
        "passed": sum(1 for entry in entries if entry["passed"]),
        "outcomes": entries,
    }


def write_report(report: dict[str, Any], report_path: Path) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    database = Path(args[0]) if args else Path(
        "governance_rule/codex/data/governance_codex.sqlite3"
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    report_path = Path(
        args[1]
    ) if len(args) > 1 else Path(
        "main-system/runtime/state"
    ) / f"formal-rule-parity-{stamp}.json"
    report = run_parity_evaluation(database)
    write_report(report, report_path)
    print(
        f"parity-run: {report['passed']}/{report['evaluated']} PASS "
        f"-> {report_path}"
    )
    for entry in report["outcomes"]:
        if not entry["passed"]:
            print(f"  FAIL {entry['rule_code']}: {entry['decision']} {entry['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
