"""Codex amendment pipeline driver tests.

The driver fills the missing orchestration between a staged request
artifact and ``ready-for-governor`` — intake (lineage lock), candidate
build, five-sovereign audit.  Publication itself stays governor-invoked
and is never exercised here.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from governance_rule.execution.codex_amendment_driver import (  # noqa: E402
    advance_all,
    advance_request,
    default_sovereign_checks,
    scan_requests,
)
from governance_rule.execution.codex_amendment_audit_gate import (  # noqa: E402
    CodexAmendmentAuditGate,
)
from governance_rule.execution.codex_amendment_lifecycle import (  # noqa: E402
    STATE_AUDITING,
    STATE_READY_FOR_GOVERNOR,
    STATE_REJECTED,
    CodexAmendmentRequestLedger,
)

_PREDECESSOR_VERSION = "2026-09-20T00:00:00Z"
_SUCCESSOR_VERSION = "2026-09-24T00:00:00Z"


def _mini_codex(path: Path) -> Path:
    database = path / "authority.sqlite3"
    connection = sqlite3.connect(str(database))
    try:
        connection.executescript(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE articles (provision_id TEXT PRIMARY KEY, rule TEXT);"
        )
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES ('codex_version', ?)",
            (_PREDECESSOR_VERSION,),
        )
        connection.execute(
            "INSERT INTO articles (provision_id, rule) VALUES ('A1', 'seed')"
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _request_payload(
    request_id: str,
    *,
    requested_by: str = "decision-sovereign",
    changes: list[dict] | None = None,
) -> dict:
    return {
        "artifact": "codex-amendment-request",
        "authority": "request-only",
        "request_id": request_id,
        "requested_by": requested_by,
        "change_class": "clarification",
        "required_review": "five-sovereign-audit-unanimous-pass",
        "flow": "A382/A488-non-disruptive-amendment-flow",
        "predecessor": {
            "codex_version": _PREDECESSOR_VERSION,
            "history_head": "test-head-001",
        },
        "not_executed": True,
        "changes": changes
        or [
            {
                "table": "articles",
                "key": {"provision_id": "A1"},
                "field": "rule",
                "proposed": "amended rule text",
            }
        ],
    }


def _stage_request(
    directory: Path, request_id: str, **overrides: object
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload = _request_payload(request_id, **overrides)
    path = directory / f"codex-amendment-request-{request_id}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


@pytest.fixture()
def ledger(tmp_path: Path) -> CodexAmendmentRequestLedger:
    return CodexAmendmentRequestLedger(tmp_path / "ledger")


@pytest.fixture()
def gate(tmp_path: Path) -> CodexAmendmentAuditGate:
    """Audit gate writing to a test ledger — never the real audit log."""
    return CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl")


@pytest.fixture()
def source_db(tmp_path: Path) -> Path:
    return _mini_codex(tmp_path)


def test_scan_requests_reports_state(
    tmp_path: Path, ledger: CodexAmendmentRequestLedger
) -> None:
    intake = tmp_path / "intake"
    path = _stage_request(intake, "scan-target")
    (intake / "codex-amendment-request-broken.json").write_text(
        json.dumps({"artifact": "something-else"}), encoding="utf-8"
    )
    results = scan_requests([intake], ledger=ledger)
    by_id = {item["request_id"]: item for item in results}
    assert by_id["scan-target"]["valid"] is True
    assert by_id["scan-target"]["state"] == ""
    assert by_id["scan-target"]["path"] == str(path)
    assert by_id["codex-amendment-request-broken"]["valid"] is False


def test_default_checks_cover_five_sovereigns(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    path = _stage_request(tmp_path / "intake", "check-shapes")
    checks = default_sovereign_checks(
        path, ledger=ledger, source_database=source_db
    )
    assert sorted(checks) == [
        "automation-sovereign",
        "decision-sovereign",
        "permission-sovereign",
        "system-runtime-sovereign",
        "xingcheng",
    ]
    decision = checks["decision-sovereign"]()
    assert decision["ok"] is True
    assert decision["evidence"]["amendment_class"] == "clarification"
    permission = checks["permission-sovereign"]()
    assert permission["ok"] is True
    runtime = checks["system-runtime-sovereign"]()
    assert runtime["ok"] is True
    # No staged candidate yet → automation receipt denies fail-closed.
    assert checks["automation-sovereign"]()["ok"] is False
    # Xingcheng assistant performs the fifth core audit from request metadata.
    xingcheng = checks["xingcheng"]()
    assert xingcheng["ok"] is True
    assert xingcheng["method"] == "xingcheng-assistant-core-audit"
    assert xingcheng["evidence"]["assistant_core_review"] == (
        "deterministic-standard-charter"
    )


def test_advance_request_reaches_ready_for_governor(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    path = _stage_request(tmp_path / "intake", "happy-path")
    result = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            successor_version=_SUCCESSOR_VERSION,
            gate=gate,
        )
    )
    assert result["ok"] is True
    assert result["state"] == STATE_READY_FOR_GOVERNOR
    assert result["build"]["ok"] is True
    audit = result["audit"]
    assert audit["ok"] is True
    assert audit["reason"] == "ALL_FIVE_SOVEREIGNS_AUDITED"
    record = ledger.load_record("happy-path")
    assert record["state"] == STATE_READY_FOR_GOVERNOR
    # Candidate carries the amendment.
    candidate = sqlite3.connect(result["build"]["candidate_path"])
    try:
        rule = candidate.execute(
            "SELECT rule FROM articles WHERE provision_id='A1'"
        ).fetchone()[0]
        version = candidate.execute(
            "SELECT value FROM metadata WHERE key='codex_version'"
        ).fetchone()[0]
    finally:
        candidate.close()
    assert rule == "amended rule text"
    assert version == _SUCCESSOR_VERSION


def test_advance_request_rejects_descriptive_changes(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    path = _stage_request(
        tmp_path / "intake",
        "descriptive-changes",
        changes=[{"note": "update A199 deadline to 40000"}],
    )
    result = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            gate=gate,
        )
    )
    assert result["ok"] is False
    assert result["stage"] == "build"
    assert result["state"] == STATE_REJECTED
    assert any(
        "CHANGE_CONTRACT_INVALID" in error
        for error in result["build"]["errors"]
    )


def test_advance_request_without_xingcheng_search_reaches_ready_for_governor(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    path = _stage_request(tmp_path / "intake", "no-search")
    result = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            search=None,
            successor_version=_SUCCESSOR_VERSION,
            gate=gate,
        )
    )
    assert result["ok"] is True
    assert result["state"] == STATE_READY_FOR_GOVERNOR
    assert result["audit"]["reason"] == "ALL_FIVE_SOVEREIGNS_AUDITED"
    record = ledger.load_record("no-search")
    assert record["state"] == STATE_READY_FOR_GOVERNOR


def test_advance_request_terminal_state_is_noop(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    path = _stage_request(tmp_path / "intake", "already-done")
    first = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            successor_version=_SUCCESSOR_VERSION,
            gate=gate,
        )
    )
    assert first["ok"] is True
    second = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            gate=gate,
        )
    )
    assert second["ok"] is True
    assert second["state"] == STATE_READY_FOR_GOVERNOR
    assert second["stage"] == "audit"


def test_advance_request_rewinds_crashed_audit(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    path = _stage_request(tmp_path / "intake", "crashed-audit")
    result = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            successor_version=_SUCCESSOR_VERSION,
            gate=gate,
        )
    )
    assert result["ok"] is True
    # Simulate a crashed second cycle by editing the record file: an
    # interrupted audit leaves the ledger at ``auditing`` with no
    # certificate.
    record_path = ledger.records_dir / "crashed-audit.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["state"] = STATE_AUDITING
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    again = asyncio.run(
        advance_request(
            path,
            ledger=ledger,
            source_database=source_db,
            gate=gate,
        )
    )
    assert again["ok"] is True
    assert again["state"] == STATE_READY_FOR_GOVERNOR


def test_advance_all_skips_terminal_and_reports(
    tmp_path: Path,
    ledger: CodexAmendmentRequestLedger,
    source_db: Path,
    gate: CodexAmendmentAuditGate,
) -> None:
    intake = tmp_path / "intake"
    _stage_request(intake, "pending-one")
    results = asyncio.run(
        advance_all(
            intake_dirs=[intake],
            ledger=ledger,
            source_database=source_db,
            successor_version=_SUCCESSOR_VERSION,
            gate=gate,
        )
    )
    assert len(results) == 1
    assert results[0]["request_id"] == "pending-one"
    assert results[0]["state"] == STATE_READY_FOR_GOVERNOR
    # Second sweep: audit already passed → the audit branch reports state.
    second = asyncio.run(
        advance_all(
            intake_dirs=[intake],
            ledger=ledger,
            source_database=source_db,
            gate=gate,
        )
    )
    assert second[0]["state"] == STATE_READY_FOR_GOVERNOR
