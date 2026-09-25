"""Fault diagnostics tests — Xingcheng read-only investigation surface."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _xingcheng_test_support import ROOT  # noqa: F401

import json
import sqlite3
from pathlib import Path

from xingcheng.infrastructure.fault_diagnostics import FaultDiagnostics


def _fake_root(tmp_path: Path) -> Path:
    (tmp_path / "main-system" / "runtime" / "state").mkdir(parents=True)
    return tmp_path


def _write_state(root: Path, name: str, payload) -> None:
    path = root / "main-system" / "runtime" / "state" / name
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_looks_like_fault_gate(tmp_path: Path) -> None:
    fd = FaultDiagnostics(_fake_root(tmp_path))
    assert fd.looks_like_fault("為什麼後端一直失敗")
    assert fd.looks_like_fault("backend crash and disconnect")
    assert not fd.looks_like_fault("你好")
    assert not fd.looks_like_fault("")


def test_new_state_files_projected(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    _write_state(root, "pending-actions.json", [
        {"action_id": "a1", "kind": "repair", "summary": "STARTUP_CRASH",
         "status": "awaiting-confirmation",
         "detail": {"failure_code": "STARTUP_CRASH", "owner": "system-runtime-sovereign"}},
    ])
    _write_state(root, "automation-switches.json",
                 {"automatic_repair_enabled": False, "updated_by": "ui"})
    _write_state(root, "hot-reload-watcher.json",
                 {"consecutive_failures": 6, "type": "channel_recovery_attempt"})
    _write_state(root, "startup-generation.json",
                 {"role": "startup-executor",
                  "result": {"ok": False, "generation_id": "g1",
                             "phases": [{"phase_id": "p2", "ok": False}]}})

    evidence = FaultDiagnostics(root).runtime_state_evidence()

    pending = evidence["pending-actions.json"]
    assert pending["awaiting_count"] == 1
    assert pending["recent"][0]["detail"]["failure_code"] == "STARTUP_CRASH"
    assert evidence["automation-switches.json"]["automatic_repair_enabled"] is False
    assert evidence["hot-reload-watcher.json"]["consecutive_failures"] == 6
    startup = evidence["startup-generation.json"]
    assert startup["ok"] is False and startup["failed_phases"] == ["p2"]


def _build_aux_root(tmp_path: Path) -> Path:
    root = _fake_root(tmp_path)
    state = root / "main-system" / "runtime" / "state"
    (state / "boot-output.log").write_text(
        "INFO ok line\nERROR IPC: no active UI shells\nTraceback boom\n",
        encoding="utf-8",
    )
    quarantine = state / "tool-crash-quarantine"
    quarantine.mkdir()
    (quarantine / "file-sorter-1.json").write_text(
        json.dumps({"tool_id": "file-sorter", "exit_code": 1}), encoding="utf-8"
    )
    _write_state(root, "pending-actions.json", [
        {"action_id": "a1", "kind": "repair", "status": "awaiting-confirmation",
         "summary": "X", "detail": {"failure_code": "X", "owner": "main-backend"}},
    ])
    _build_learning_db(root)
    return root


def _build_learning_db(root: Path) -> None:
    learning_dir = root / "main-system" / "data" / "automatic-repair"
    learning_dir.mkdir(parents=True)
    db = sqlite3.connect(learning_dir / "repair-learning.sqlite3")
    db.execute(
        "CREATE TABLE error_signatures (signature_hash TEXT, error_class TEXT, "
        "message_pattern TEXT, failure_code TEXT, file_context TEXT, "
        "target_tool_id TEXT, first_seen TEXT, last_seen TEXT, occurrence_count INTEGER)"
    )
    db.execute(
        "CREATE TABLE repair_outcomes (outcome_id TEXT, run_id TEXT, "
        "signature_hash TEXT, remedy TEXT, ok INTEGER, detail_json TEXT, recorded_at TEXT)"
    )
    db.execute(
        "CREATE TABLE learned_recipes (recipe_id TEXT, name TEXT, "
        "failure_signatures_json TEXT, remedy TEXT, owner TEXT, automatic INTEGER, "
        "runtime_only INTEGER, learned_at TEXT, occurrence_count INTEGER, "
        "success_rate REAL, source TEXT)"
    )
    db.execute(
        "INSERT INTO error_signatures VALUES ('s1','E','m','IPC_DEAD','','ipc-channel','','now',7)"
    )
    db.execute(
        "INSERT INTO repair_outcomes VALUES ('o1','r1','s1','x',0,'{}','now')"
    )
    db.commit()
    db.close()


def test_aux_evidence_readers(tmp_path: Path) -> None:
    fd = FaultDiagnostics(_build_aux_root(tmp_path))
    assert fd.boot_log_tail()["lines"] == [
        "ERROR IPC: no active UI shells", "Traceback boom",
    ]
    assert fd.quarantined_tools()[0]["tool_id"] == "file-sorter"
    learning = fd.repair_learning_tail()
    assert learning["available"] is True
    assert learning["recurring_signatures"][0]["occurrence_count"] == 7


def test_diagnose_merges_aux_evidence(tmp_path: Path) -> None:
    result = FaultDiagnostics(_build_aux_root(tmp_path)).diagnose("後端斷線")
    loc = result["localization"]
    sources = {a["source"] for a in loc["anomalies"]}
    assert "pending-actions.json" in sources
    assert "tool-crash-quarantine" in sources
    assert "repair-learning.sqlite3" in sources
    entities = {a["entity"] for a in loc["anomalies"]}
    assert "tool-runtime" in entities
    assert "ipc-channel" in entities
    assert result["log_errors"]
    assert result["quarantined_tools"]
    assert result["automation_context"] == {}
    assert result["authority"]["execution"] is False
