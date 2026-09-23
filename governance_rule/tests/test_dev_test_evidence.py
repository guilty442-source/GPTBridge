"""Dev-test evidence ledger: non-gate pytest results bound to source
revision, contract hash and an append-only ledger."""
from __future__ import annotations

import json
import os
from pathlib import Path

from governance_rule.execution.audit.dev_test_evidence import (
    record_pytest_run,
    record_test_run,
    semantic_evidence_status,
)

ROOT = Path(__file__).resolve().parents[2]

_PROBE_REL = "governance_rule/tests/test_devtest_probe_tmp.py"

_PROBE_SRC = (
    "def test_probe() -> None:\n"
    "    assert True\n"
)


def _probe(outcome: str) -> tuple[str, Path]:
    # Probes must live outside the collected test tree: a transient
    # test_*.py inside governance_rule/tests races with concurrent
    # suite runs (collection ImportError) and with the audit sql-scan.
    # runtime/ is skipped by both. The pid keeps the module name valid
    # and unique across parallel workers.
    rel = (
        "main-system/runtime/temp/devtest-probes/"
        f"test_devtest_probe_{outcome}_{os.getpid()}_tmp.py"
    )
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    return rel, path


def test_record_test_run_binds_revision_and_contract(tmp_path: Path) -> None:
    ledger = tmp_path / "evidence.jsonl"
    entry = record_test_run(
        ROOT,
        kind="pytest",
        files={_PROBE_REL: "ab" * 32},
        verdict="pass",
        totals={"tests": 1, "passed": 1},
        ledger=ledger,
    )
    assert entry["schema"] == "dev-test-evidence/v1"
    assert entry["gate_role"] == "non-gate"
    assert entry["source_revision"]  # bound to HEAD
    assert entry["contract_hash"]
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["files"][_PROBE_REL] == "ab" * 32


def test_pytest_run_records_and_status_tracks_freshness(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "evidence.jsonl"
    rel, probe = _probe("pass")
    probe.write_text(_PROBE_SRC, encoding="utf-8")
    try:
        entry = record_pytest_run(
            ROOT, [rel], budget_s=120.0, ledger=ledger
        )
        assert entry["verdict"] == "pass"
        assert entry["totals"]["tests"] == 1
        assert rel in entry["files"]

        status = semantic_evidence_status(ROOT, [rel], ledger=ledger)
        assert status["fresh"] == [rel]
        assert status["complete"] is True

        # Content change invalidates the bound contract hash → stale,
        # not silently still-valid.
        probe.write_text(
            _PROBE_SRC + "def test_probe_2() -> None:\n    assert True\n",
            encoding="utf-8",
        )
        status = semantic_evidence_status(ROOT, [rel], ledger=ledger)
        assert status["stale"] == [rel]
        assert status["complete"] is False
    finally:
        probe.unlink(missing_ok=True)


def test_missing_evidence_is_incomplete_not_invalid(tmp_path: Path) -> None:
    ledger = tmp_path / "evidence.jsonl"
    status = semantic_evidence_status(
        ROOT,
        ["governance_rule/tests/test_dev_test_evidence.py"],
        ledger=ledger,
    )
    assert status["missing"] == [
        "governance_rule/tests/test_dev_test_evidence.py"
    ]
    assert status["complete"] is False


def test_failed_pytest_run_still_records_fail_visible(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "evidence.jsonl"
    rel, probe = _probe("fail")
    probe.write_text(
        "def test_probe() -> None:\n    assert False\n",
        encoding="utf-8",
    )
    try:
        entry = record_pytest_run(
            ROOT, [rel], budget_s=120.0, ledger=ledger
        )
        assert entry["verdict"] == "fail"
        assert entry["failed_nodes"]
        # A fail verdict can never back fresh PASS evidence.
        status = semantic_evidence_status(ROOT, [rel], ledger=ledger)
        assert status["fresh"] == []
        assert status["complete"] is False
    finally:
        probe.unlink(missing_ok=True)
