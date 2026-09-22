"""Worker-side Codex amendment toolchain contracts (G69-G71)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from governance_rule.execution import formal_rules  # noqa: E402
from governance_rule.execution.codex_amendment_audit_gate import (  # noqa: E402
    CodexAmendmentAuditGate,
)
from governance_rule.execution.codex_amendment_audit_runner import (  # noqa: E402
    run_five_sovereign_audit,
)
from governance_rule.execution.codex_amendment_executor import (  # noqa: E402
    CodexAmendmentDenied,
    execute_amendment,
)
from governance_rule.execution.codex_amendment_contract import (  # noqa: E402
    CONTENT_HASH_ALGORITHM,
    RULE_STATE_DECLARED_PENDING_PARITY,
    RULE_STATE_PROPOSED,
    content_hash,
    compute_seal_preview,
    revision_entry_hash,
    search_document_hash,
    validate_rule_state,
    validate_rule_transition,
)
from governance_rule.execution.codex_amendment_lifecycle import (  # noqa: E402
    AmendmentLifecycleError,
    CodexAmendmentRequestLedger,
    STATE_READY_FOR_GOVERNOR,
    STATE_REJECTED,
    STATE_SUCCESSOR_BUILT,
)
from governance_rule.execution.codex_successor_builder import (  # noqa: E402
    build_successor,
)

VERSION = "2026-09-20T00:00:00Z"
SUCCESSOR = "2026-09-20T01:00:00Z"
HISTORY_HEAD = "a" * 64


def _database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE articles (
            position INTEGER PRIMARY KEY,
            provision_id TEXT,
            subject TEXT,
            rule TEXT,
            prohibition TEXT,
            exception TEXT
        );
        CREATE TABLE project_registry (
            code TEXT PRIMARY KEY,
            value TEXT,
            introduced_version TEXT
        );
        CREATE TABLE formal_rule_registry (
            rule_code TEXT PRIMARY KEY,
            status TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO metadata VALUES ('codex_version', ?)", (VERSION,)
    )
    connection.execute(
        "INSERT INTO articles VALUES (1, 'A900', 'subject', 'rule', '', '')"
    )
    connection.execute(
        "INSERT INTO project_registry VALUES ('EXISTING', 'old', ?)",
        (VERSION,),
    )
    connection.commit()
    connection.close()
    return path


def _request(
    path: Path,
    *,
    request_id: str = "codex-amendment-request-test",
    predecessor_version: str = VERSION,
    history_head: str = HISTORY_HEAD,
    revision_sequence: int = 1,
) -> Path:
    payload = {
        "artifact": "codex-amendment-request",
        "request_id": request_id,
        "authority": "request-only",
        "not_executed": True,
        "requested_by": "human-governor instruction",
        "change_class": "architecture-authority",
        "required_review": "full-architecture-review",
        "predecessor": {
            "codex_version": predecessor_version,
            "history_head": history_head,
            "revision_sequence": revision_sequence,
        },
        "changes": [
            {
                "table": "project_registry",
                "key": {"code": "EXISTING"},
                "field": "value",
                "proposed": "new",
            }
        ],
        "proposed_successors": [
            {
                "registry": "project_registry",
                "action": "insert",
                "rows": [
                    {
                        "code": "NEW_COMPONENT",
                        "value": "registered",
                        "introduced_version": "successor",
                    }
                ],
            },
            {
                "registry": "architecture_diagram_artifact_registry",
                "action": "rebind",
                "note": "artifact regeneration remains governor-side",
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _passing_checks() -> dict[str, object]:
    return {
        "decision-sovereign": lambda: {
            "ok": True,
            "method": "decision-precedence-audit",
            "evidence": {
                "amendment_class": "architecture-authority",
                "basis_references": ["A382"],
                "successor_scope_unique": True,
            },
        },
        "permission-sovereign": lambda: {
            "ok": True,
            "method": "directory-identity-audit",
            "evidence": {
                "directory_rows": ["project_registry"],
                "identity_lifecycle_parity": True,
                "testflow_references": ["TS_TEST"],
            },
        },
        "system-runtime-sovereign": lambda: {
            "ok": True,
            "method": "runtime-reader-audit",
            "evidence": {
                "reader_generation_plan": "drain-then-publish",
                "channel_continuity": True,
                "health_window": 300,
            },
        },
        "automation-sovereign": lambda: {
            "ok": True,
            "method": "staging-seal-audit",
            "evidence": {
                "staging_isolation": True,
                "seal_roots": {"preview": True},
                "mirror_chain": True,
                "version_identity": SUCCESSOR,
                "rollback_pointer": True,
            },
        },
        "xingcheng": lambda: {
            "ok": True,
            "method": "xingcheng-web-search",
            "network_search": True,
            "evidence": {
                "network_search": {"queries": 1, "responses": 1},
                "source_classification": ["xingcheng-web-search"],
                "redaction_check": "metadata-only",
            },
        },
    }


def test_canonical_hash_contracts_are_deterministic() -> None:
    payload = {"b": [2, "規則"], "a": {"x": 1}}
    first = content_hash(payload)
    assert CONTENT_HASH_ALGORITHM == "sha256-canonical-json-utf8-v1"
    assert first == content_hash({"a": {"x": 1}, "b": [2, "規則"]})
    assert len(first) == 64
    assert search_document_hash("文件") == search_document_hash("文件".encode())
    entry = {
        "sequence": 2,
        "version": SUCCESSOR,
        "previous_hash": HISTORY_HEAD,
        "entry_hash": "ignored",
    }
    assert revision_entry_hash(entry) == revision_entry_hash(
        {k: v for k, v in entry.items() if k != "entry_hash"}
    )


def test_rule_state_contract_requires_evaluator_and_explicit_transition() -> None:
    assert validate_rule_state(
        RULE_STATE_PROPOSED, evaluator_registered=False
    ) == ("RULE_EVALUATOR_REQUIRED",)
    assert validate_rule_state(
        RULE_STATE_PROPOSED, evaluator_registered=True
    ) == ()
    assert validate_rule_transition(
        RULE_STATE_PROPOSED,
        RULE_STATE_DECLARED_PENDING_PARITY,
        evaluator_registered=True,
    ) == ()
    assert validate_rule_transition(
        RULE_STATE_PROPOSED,
        "active",
        evaluator_registered=True,
        parity_evidence=True,
    ) == ("RULE_STATE_TRANSITION_DENIED:proposed->active",)
    assert validate_rule_state("active", evaluator_registered=None) == (
        "RULE_EVALUATOR_REQUIRED",
    )


def test_request_lineage_lock_serializes_same_generation(tmp_path: Path) -> None:
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    first = _request(tmp_path / "first.json", request_id="request-one")
    second = _request(tmp_path / "second.json", request_id="request-two")

    record = ledger.begin(first)
    assert record.state == "submitted"
    with pytest.raises(AmendmentLifecycleError) as locked:
        ledger.begin(second)
    assert locked.value.code == "REQUEST_LINEAGE_LOCKED"

    ledger.reject("request-one", reason="superseded")
    replacement = ledger.begin(second)
    assert replacement.request_id == "request-two"


def test_stale_or_missing_lineage_is_fail_closed(tmp_path: Path) -> None:
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    stale = _request(
        tmp_path / "stale.json", predecessor_version="2026-09-19T00:00:00Z"
    )
    with pytest.raises(AmendmentLifecycleError) as stale_error:
        ledger.begin(stale, current_version=VERSION)
    assert stale_error.value.code == "STALE_PREDECESSOR_VERSION"

    broken = tmp_path / "broken.json"
    payload = json.loads(_request(tmp_path / "ok.json").read_text("utf-8"))
    payload["predecessor"].pop("history_head")
    broken.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AmendmentLifecycleError) as missing:
        ledger.begin(broken)
    assert missing.value.code == "REQUEST_LINEAGE_REQUIRED"
    record = ledger.load_record("codex-amendment-request-test")
    assert record["state"] == STATE_REJECTED
    assert record["not_executed"] is True


def test_invalid_lifecycle_transition_is_denied(tmp_path: Path) -> None:
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    request = _request(tmp_path / "request.json")
    ledger.begin(request)
    with pytest.raises(AmendmentLifecycleError) as denied:
        ledger.transition("codex-amendment-request-test", "executed")
    assert denied.value.code == "REQUEST_STATE_TRANSITION_DENIED"


def test_successor_builder_applies_explicit_rows_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path / "source.sqlite3")
    request = _request(tmp_path / "request.json")
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    output = tmp_path / "candidate.sqlite3"
    source_bytes = source.read_bytes()

    result = build_successor(
        request,
        source,
        output,
        ledger=ledger,
        successor_version=SUCCESSOR,
        expected_current_version=VERSION,
        expected_revision_sequence=1,
    )

    assert result.ok is True
    assert result.candidate_sha256
    assert result.manifest_path.endswith(".candidate-manifest.json")
    assert source.read_bytes() == source_bytes
    connection = sqlite3.connect(output)
    assert connection.execute(
        "SELECT value FROM metadata WHERE key='codex_version'"
    ).fetchone()[0] == SUCCESSOR
    assert connection.execute(
        "SELECT value FROM project_registry WHERE code='EXISTING'"
    ).fetchone()[0] == "new"
    assert connection.execute(
        "SELECT introduced_version FROM project_registry WHERE code='NEW_COMPONENT'"
    ).fetchone()[0] == SUCCESSOR
    connection.close()
    manifest = json.loads(Path(result.manifest_path).read_text("utf-8"))
    assert manifest["governor_only"] == [
        "seal_manifest",
        "epoch_seal_manifest",
        "revision_history",
        "external-signatures",
        "atomic-publication",
        "authority-reanchor",
    ]
    assert manifest["seal_preview"]["schema"] == "gptbridge-codex-seal-preview/v1"
    record = ledger.load_record("codex-amendment-request-test")
    assert record["state"] == STATE_SUCCESSOR_BUILT


def test_builder_accepts_legacy_explicit_proposed_change_rows(tmp_path: Path) -> None:
    source = _database(tmp_path / "source.sqlite3")
    request = _request(tmp_path / "request.json")
    payload = json.loads(request.read_text("utf-8"))
    payload.pop("changes")
    payload.pop("proposed_successors")
    payload["proposed_change"] = {
        "table": "project_registry",
        "operation": "insert one additive registry row",
        "rows": [
            {
                "code": "LEGACY_COMPONENT",
                "value": "registered",
                "introduced_version": "<successor-version>",
            }
        ],
    }
    request.write_text(json.dumps(payload), encoding="utf-8")
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")

    result = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )

    assert result.ok is True
    connection = sqlite3.connect(tmp_path / "candidate.sqlite3")
    assert connection.execute(
        "SELECT introduced_version FROM project_registry WHERE code='LEGACY_COMPONENT'"
    ).fetchone()[0] == SUCCESSOR
    connection.close()


def test_builder_rejects_unknown_schema_and_closes_lineage(tmp_path: Path) -> None:
    source = _database(tmp_path / "source.sqlite3")
    request = _request(tmp_path / "request.json")
    payload = json.loads(request.read_text("utf-8"))
    payload["proposed_successors"][0]["rows"][0]["unknown_column"] = "x"
    request.write_text(json.dumps(payload), encoding="utf-8")
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")

    result = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )

    assert result.ok is False
    assert "CANDIDATE_COLUMN_UNKNOWN" in result.errors[0]
    assert ledger.load_record("codex-amendment-request-test")["state"] == STATE_REJECTED
    assert not (tmp_path / "ledger" / "lineage-locks").exists() or not list(
        (tmp_path / "ledger" / "lineage-locks").iterdir()
    )


def test_builder_rejects_invalid_formal_rule_state_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path / "source.sqlite3")
    connection = sqlite3.connect(source)
    connection.execute(
        "INSERT INTO formal_rule_registry VALUES ('FR-TEST', 'proposed')"
    )
    connection.commit()
    connection.close()
    request = _request(tmp_path / "request.json")
    payload = json.loads(request.read_text("utf-8"))
    payload["changes"] = [
        {
            "table": "formal_rule_registry",
            "key": {"rule_code": "FR-TEST"},
            "field": "status",
            "proposed": "active",
            "also": {"parity_evidence_id": "parity-evidence"},
        }
    ]
    payload.pop("proposed_successors")
    request.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        formal_rules,
        "registered_rule_codes",
        lambda: frozenset({"FR-TEST"}),
    )
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")

    result = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )

    assert result.ok is False
    assert "RULE_STATE_TRANSITION_INVALID" in result.errors[0]
    assert "proposed->active" in result.errors[0]
    assert ledger.load_record("codex-amendment-request-test")["state"] == STATE_REJECTED


def test_seal_preview_recomputes_deterministically(tmp_path: Path) -> None:
    database = _database(tmp_path / "candidate.sqlite3")
    first = compute_seal_preview(database)
    second = compute_seal_preview(database)
    assert first == second
    assert len(first["content_root"]) == 64
    assert len(first["identity_root"]) == 64
    assert len(first["full_root"]) == 64


@pytest.mark.asyncio
async def test_audit_runner_requires_built_successor(tmp_path: Path) -> None:
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    request = _request(tmp_path / "request.json")
    ledger.begin(request)

    result = await run_five_sovereign_audit(
        request,
        _passing_checks(),
        ledger=ledger,
        gate=CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl"),
    )

    assert result.ok is False
    assert "SUCCESSOR_NOT_BUILT" in result.error
    assert ledger.load_record("codex-amendment-request-test")["state"] == STATE_REJECTED


@pytest.mark.asyncio
async def test_audit_runner_rejects_missing_sovereign(tmp_path: Path) -> None:
    source = _database(tmp_path / "source.sqlite3")
    request = _request(tmp_path / "request.json")
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    built = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )
    assert built.ok
    checks = _passing_checks()
    checks.pop("xingcheng")

    result = await run_five_sovereign_audit(
        request,
        checks,
        ledger=ledger,
        gate=CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl"),
    )

    assert result.ok is False
    assert result.result is not None
    assert result.result.reason == "MISSING_SOVEREIGN_AUDIT"
    assert ledger.load_record("codex-amendment-request-test")["state"] == STATE_REJECTED


@pytest.mark.asyncio
async def test_audit_runner_passes_and_marks_ready_for_governor(
    tmp_path: Path,
) -> None:
    source = _database(tmp_path / "source.sqlite3")
    request = _request(tmp_path / "request.json")
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")
    built = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )
    assert built.ok

    result = await run_five_sovereign_audit(
        request,
        _passing_checks(),
        ledger=ledger,
        gate=CodexAmendmentAuditGate(ledger_path=tmp_path / "audit.jsonl"),
    )

    assert result.ok is True
    assert result.state == STATE_READY_FOR_GOVERNOR
    assert result.result is not None
    assert result.result.certificate is not None
    serialized = result.as_dict()
    assert serialized["ok"] is True
    assert serialized["audit_recorded"] is True
    assert (
        serialized["certificate"]["amendment_id"]
        == "codex-amendment-request-test"
    )
    record = ledger.load_record("codex-amendment-request-test")
    assert record["state"] == STATE_READY_FOR_GOVERNOR
    assert record["not_executed"] is True


def test_executor_rejects_audit_result_for_another_request(tmp_path: Path) -> None:
    source = _database(tmp_path / "prepared.sqlite3")
    request = _request(tmp_path / "request.json")

    with pytest.raises(CodexAmendmentDenied) as denied:
        execute_amendment(
            request_path=request,
            prepared_database=source,
            audit_result={
                "amendment_id": "other-request",
                "ok": True,
                "audit_recorded": True,
                "certificate": {
                    "schema": "gptbridge.codex-amendment-certificate/v1",
                    "amendment_id": "other-request",
                },
            },
            apply=True,
            codex_root=tmp_path / "codex",
            staging_root=tmp_path / "staging",
        )

    assert "FIVE_SOVEREIGN_AUDIT_AMENDMENT_MISMATCH" in str(denied.value)


def _parity_database(path: Path, *, evidence: str) -> Path:
    source = _database(path)
    connection = sqlite3.connect(source)
    connection.execute(
        "ALTER TABLE formal_rule_registry ADD COLUMN parity_status TEXT"
    )
    connection.execute(
        "ALTER TABLE formal_rule_registry ADD COLUMN parity_evidence_id TEXT"
    )
    connection.execute(
        "INSERT INTO formal_rule_registry VALUES "
        "('FR-VERIFIED', 'evaluator-parity-verified', 'VERIFIED', ?)",
        (evidence,),
    )
    connection.commit()
    connection.close()
    return source


def test_verified_rule_state_reads_stored_parity_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_formal_rule_errors must honor per-row parity evidence (G67)."""
    source = _parity_database(tmp_path / "source.sqlite3", evidence="ev-1")
    connection = sqlite3.connect(source)
    connection.commit()
    connection.close()
    request = _request(tmp_path / "request.json")
    payload = json.loads(request.read_text("utf-8"))
    payload.pop("proposed_successors")
    request.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        formal_rules,
        "registered_rule_codes",
        lambda: frozenset({"FR-VERIFIED"}),
    )
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")

    result = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )

    assert result.ok is True


def test_verified_rule_state_without_evidence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _parity_database(tmp_path / "source.sqlite3", evidence="")
    connection = sqlite3.connect(source)
    connection.execute(
        "UPDATE formal_rule_registry SET parity_status='PENDING'"
    )
    connection.commit()
    connection.close()
    request = _request(tmp_path / "request.json")
    payload = json.loads(request.read_text("utf-8"))
    payload.pop("proposed_successors")
    request.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        formal_rules,
        "registered_rule_codes",
        lambda: frozenset({"FR-VERIFIED"}),
    )
    ledger = CodexAmendmentRequestLedger(tmp_path / "ledger")

    result = build_successor(
        request,
        source,
        tmp_path / "candidate.sqlite3",
        ledger=ledger,
        successor_version=SUCCESSOR,
    )

    assert result.ok is False
    assert "RULE_PARITY_EVIDENCE_REQUIRED" in ",".join(result.errors)
