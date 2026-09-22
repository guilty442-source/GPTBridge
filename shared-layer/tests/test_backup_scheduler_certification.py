"""BackupScheduler -> restore-certification wiring (G24 DoD).

The scheduler must record every completed backup into
``gptbridge_index.backup_catalog`` (migration 016) with a real sha256,
source generation, schema version, and the restore-certification verdict.
Certification is fail-closed: no certifier or a raising certifier leaves
``restore_certified=false`` rather than fabricating evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.database.backup import BackupResult
from shared_layer.database.backup_scheduler import BackupScheduler, ScheduledBackup
from shared_layer.database.restore_certification import (
    CertificationCheck,
    RestoreCertificationResult,
)


class _FakeOrchestrator:
    def __init__(self, result: BackupResult):
        self._result = result

    def backup(self, destination):  # noqa: ARG002 - signature fixed by caller
        return self._result


class _FakeCursorConn:
    """Minimal psycopg-style connection capturing execute() calls."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self

    def fetchone(self):
        sql = self.calls[-1][0]
        if "current_backend_generation" in sql:
            return (7,)
        if "schema_version" in sql:
            return ("105_backup_restore_orchestration.sql",)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _scheduler(tmp_path, certifier=None):
    class _Settings:
        admin_dsn = "postgresql://unused"
        database = "gptbridge"
        backup_root = str(tmp_path)

    sched = BackupScheduler(
        _Settings(),
        orchestrator=_FakeOrchestrator(
            BackupResult(path=tmp_path / "dump.bin", size=0)
        ),
        backup_root=str(tmp_path),
        restore_certifier=certifier,
    )
    conn = _FakeCursorConn()
    sched._get_connection = lambda: conn
    return sched, conn


def _catalog_insert(conn):
    for sql, params in conn.calls:
        if "INSERT INTO gptbridge_index.backup_catalog" in sql:
            return params
    return None


def _make_backup(tmp_path):
    payload = tmp_path / "dump.bin"
    payload.write_bytes(b"backup-bytes")
    return payload


def test_catalog_insert_targets_index_schema(tmp_path):
    """Regression: the old code wrote gptbridge_audit.backup_catalog,
    a table no migration creates."""
    _make_backup(tmp_path)
    sched, conn = _scheduler(tmp_path)
    job = ScheduledBackup(engine="postgresql", rpo_seconds=60)
    assert sched._run_backup(job) is True
    params = _catalog_insert(conn)
    assert params is not None
    assert not any(
        "gptbridge_audit.backup_catalog" in sql for sql, _ in conn.calls
    )


def test_uncertified_when_no_certifier(tmp_path):
    _make_backup(tmp_path)
    sched, conn = _scheduler(tmp_path)
    job = ScheduledBackup(engine="postgresql", rpo_seconds=60)
    assert sched._run_backup(job) is True
    params = _catalog_insert(conn)
    # restore_tested_at, restore_certified, restore_certification
    assert params[7] is None
    assert params[8] is False
    assert job.consecutive_failures == 0


def test_certifier_pass_marks_certified(tmp_path):
    _make_backup(tmp_path)
    ok = RestoreCertificationResult(
        passed=True,
        checks=[CertificationCheck("restore_drill", True, "ready")],
        generation_before=7,
        generation_after=8,
    )
    sched, conn = _scheduler(tmp_path, certifier=lambda path: ok)
    job = ScheduledBackup(engine="postgresql", rpo_seconds=60)
    assert sched._run_backup(job) is True
    params = _catalog_insert(conn)
    assert params[7] is not None
    assert params[8] is True
    import json

    cert = json.loads(params[9])
    assert cert["passed"] is True
    assert cert["checks"][0]["name"] == "restore_drill"
    assert job.consecutive_failures == 0


def test_certifier_failure_counts_as_failure(tmp_path):
    _make_backup(tmp_path)
    bad = RestoreCertificationResult(passed=False, checks=[])
    sched, conn = _scheduler(tmp_path, certifier=lambda path: bad)
    job = ScheduledBackup(engine="postgresql", rpo_seconds=60)
    assert sched._run_backup(job) is True  # backup itself succeeded
    params = _catalog_insert(conn)
    assert params[8] is False
    assert job.consecutive_failures == 1


def test_certifier_exception_fails_closed(tmp_path):
    _make_backup(tmp_path)

    def boom(path):
        raise RuntimeError("scratch instance unavailable")

    sched, conn = _scheduler(tmp_path, certifier=boom)
    job = ScheduledBackup(engine="postgresql", rpo_seconds=60)
    assert sched._run_backup(job) is True
    params = _catalog_insert(conn)
    assert params[8] is False
    assert job.consecutive_failures == 1


def test_backup_hash_is_real_sha256(tmp_path):
    payload = _make_backup(tmp_path)
    import hashlib

    expected = hashlib.sha256(payload.read_bytes()).hexdigest()
    sched, conn = _scheduler(tmp_path)
    job = ScheduledBackup(engine="postgresql", rpo_seconds=60)
    sched._run_backup(job)
    params = _catalog_insert(conn)
    assert params[5] == expected  # backup_hash column
    assert params[3] == 7  # source_generation from probe
    assert params[4] == "105_backup_restore_orchestration.sql"


def test_status_reports_certifier_presence(tmp_path):
    sched, _ = _scheduler(tmp_path)
    assert sched.get_status()["restore_certifier"] is False
    sched2, _ = _scheduler(tmp_path, certifier=lambda p: None)
    assert sched2.get_status()["restore_certifier"] is True


def test_externally_driven_run_once(tmp_path):
    """§1.1 自動化集中：start(spawn_loop=False) runs no private thread;
    each run_once performs one due-check iteration (automation core
    drives the cadence)."""
    _make_backup(tmp_path)
    sched, conn = _scheduler(tmp_path)
    sched._jobs["postgresql"] = ScheduledBackup(
        engine="postgresql", rpo_seconds=3600,
    )
    sched.start(spawn_loop=False)
    try:
        assert sched._thread is None
        sched._jobs["postgresql"].next_backup = None  # force due
        sched.run_once()
        job = sched._jobs["postgresql"]
        assert job.last_backup is not None
        assert _catalog_insert(conn) is not None
    finally:
        sched.stop()
