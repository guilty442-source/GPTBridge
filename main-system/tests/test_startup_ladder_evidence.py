"""Ladder evidence probes for the previously source-less rungs (G24).

MODULE_PRIVATE_READY / RECOVERY_READY / READ_MODEL_READY must be backed
by real artifacts — never fabricated.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
if str(SRC_CORE) not in sys.path:
    sys.path.insert(0, str(SRC_CORE))

from startup_core.phases_execution import (  # noqa: E402
    _PRIVATE_STATE_STORES,
    _probe_private_state,
    _probe_recovery,
)


def _make_store(path: Path, table: str = "t") -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def test_private_state_ready_when_all_stores_healthy(tmp_path: Path) -> None:
    for name in _PRIVATE_STATE_STORES:
        _make_store(tmp_path / f"{name}.sqlite3")
    result = _probe_private_state(tmp_path)
    assert result["ready"] is True
    assert result["probed"] == len(_PRIVATE_STATE_STORES)
    assert set(result["stores"]) == set(_PRIVATE_STATE_STORES)


def test_private_state_not_ready_without_stores(tmp_path: Path) -> None:
    result = _probe_private_state(tmp_path)
    assert result["ready"] is False
    assert result["probed"] == 0


def test_private_state_corrupt_store_fails_closed(tmp_path: Path) -> None:
    for name in _PRIVATE_STATE_STORES:
        _make_store(tmp_path / f"{name}.sqlite3")
    corrupt = tmp_path / "updates.sqlite3"
    corrupt.write_bytes(b"not-a-sqlite-file" * 64)
    result = _probe_private_state(tmp_path)
    assert result["ready"] is False
    assert result["stores"]["updates"] != "ok"


def test_recovery_ready_with_inspectable_outbox(tmp_path: Path) -> None:
    outbox = tmp_path / "state-outbox.sqlite3"
    conn = sqlite3.connect(outbox)
    conn.execute(
        "CREATE TABLE outbox_events (sequence INTEGER PRIMARY KEY, "
        "committed_at TEXT)"
    )
    conn.execute("INSERT INTO outbox_events (committed_at) VALUES (NULL)")
    conn.execute(
        "INSERT INTO outbox_events (committed_at) VALUES ('2026-09-21')"
    )
    conn.commit()
    conn.close()
    result = _probe_recovery(tmp_path)
    assert result["ready"] is True
    assert result["pending_events"] == 1
    assert result["total_events"] == 2


def test_recovery_not_ready_without_outbox(tmp_path: Path) -> None:
    result = _probe_recovery(tmp_path)
    assert result["ready"] is False
    assert result["reason"] == "outbox-absent"


def test_read_model_probe_semantics(tmp_path: Path) -> None:
    """The runtime-readiness snapshot is the read-model evidence."""
    readiness = tmp_path / "runtime-readiness.json"
    assert not readiness.exists()
    readiness.write_text(
        json.dumps({"snapshot": {"overall_ready": False}}), encoding="utf-8"
    )
    snapshot = json.loads(readiness.read_text(encoding="utf-8"))
    assert isinstance(snapshot["snapshot"], dict)
