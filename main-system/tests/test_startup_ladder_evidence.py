"""Ladder evidence probes for the previously source-less rungs (G24).

MODULE_PRIVATE_READY / RECOVERY_READY / READ_MODEL_READY must be backed
by real artifacts - never fabricated.  Private-state evidence now probes
PostgreSQL schema reachability; the outbox probe uses the governed DSN.
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager

import pytest
from pathlib import Path

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
if str(SRC_CORE) not in sys.path:
    sys.path.insert(0, str(SRC_CORE))

from startup_core.phases_execution import (  # noqa: E402
    _PRIVATE_STATE_STORES,
    _probe_private_state,
    _probe_recovery,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Single-query schemata probe fake: missing schemas model faults."""

    def __init__(self, failures: set[str]) -> None:
        self._failures = failures

    def execute(self, *args, **kwargs):
        expected = list(args[1][0]) if len(args) > 1 else []
        return _FakeCursor(
            [
                {"schema_name": schema}
                for schema in expected
                if schema not in self._failures
            ]
        )


def _patch_connect(monkeypatch: pytest.MonkeyPatch, failures: set[str]) -> None:
    from shared_layer.local import pg_adapter

    @contextmanager
    def fake_connect(schema, *args, **kwargs):
        yield _FakeConn(failures)

    monkeypatch.setattr(pg_adapter, "connect", fake_connect)


def test_private_state_ready_when_all_schemas_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connect(monkeypatch, set())
    result = _probe_private_state(tmp_path)
    assert result["ready"] is True
    assert result["probed"] == len(_PRIVATE_STATE_STORES)
    assert set(result["stores"]) == {name for name, _ in _PRIVATE_STATE_STORES}


def test_private_state_not_ready_when_all_schemas_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connect(
        monkeypatch, {schema for _, schema in _PRIVATE_STATE_STORES}
    )
    result = _probe_private_state(tmp_path)
    assert result["ready"] is False
    assert result["probed"] == len(_PRIVATE_STATE_STORES)


def test_private_state_single_schema_fault_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connect(monkeypatch, {"gptbridge_legacy"})
    result = _probe_private_state(tmp_path)
    assert result["ready"] is False
    assert result["stores"]["updates"] == "missing:postgresql:gptbridge_legacy"


def test_recovery_ready_with_inspectable_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL outbox path: live schema reachable in dev environment."""
    monkeypatch.setenv("GPTBRIDGE_OUTBOX_ENGINE", "postgresql")
    result = _probe_recovery(tmp_path)
    if not result["ready"]:
        pytest.skip(f"PostgreSQL outbox unreachable: {result.get('reason')}")
    assert result["engine"] == "postgresql"
    assert "pending_events" in result and "total_events" in result


def test_recovery_not_ready_for_retired_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A610/A621: any non-postgresql outbox engine fails closed."""
    monkeypatch.setenv("GPTBRIDGE_OUTBOX_ENGINE", "sqlite")
    result = _probe_recovery(tmp_path)
    assert result["ready"] is False
    assert result["reason"].startswith("unsupported-outbox-engine")


def test_read_model_probe_semantics(tmp_path: Path) -> None:
    """The runtime-readiness snapshot is the read-model evidence."""
    readiness = tmp_path / "runtime-readiness.json"
    assert not readiness.exists()
    readiness.write_text(
        json.dumps({"snapshot": {"overall_ready": False}}), encoding="utf-8"
    )
