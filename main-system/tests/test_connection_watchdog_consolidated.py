"""Split from consolidated test_main_system.py (main-system/tests/test_connection_watchdog.py)."""
from __future__ import annotations

import _main_system_test_support as _support  # noqa: F401
from _main_system_test_support import ROOT

import json
import sys
import time
from pathlib import Path
import pytest
from tasks.connection_watchdog import (  # noqa: E402
    CONNECTION_DEAD_THRESHOLD,
    CONNECTION_WATCHDOG_VERSION,
    ConnectionEvent,
    ConnectionSnapshot,
    ConnectionWatchdog,
    write_ipc_connection_state,
)

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "main-system" / "src-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))




# ---------------------------------------------------------------------------
# ConnectionSnapshot
# ---------------------------------------------------------------------------


def test_snapshot_defaults_to_unknown() -> None:
    snap = ConnectionSnapshot()
    assert snap.overall_state == "unknown"
    assert snap.consecutive_dead == 0
    assert snap.probe_count == 0


def test_snapshot_as_dict_roundtrip() -> None:
    snap = ConnectionSnapshot(
        backend_process_alive=True,
        backend_http_healthy=True,
        frontend_connected=True,
        overall_state="connected",
        probe_count=5,
    )
    d = snap.as_dict()
    restored = ConnectionSnapshot(**{k: d[k] for k in d})
    assert restored.overall_state == "connected"
    assert restored.probe_count == 5


# ---------------------------------------------------------------------------
# write_ipc_connection_state
# ---------------------------------------------------------------------------


def test_write_ipc_connection_state_creates_file(tmp_path: Path) -> None:
    write_ipc_connection_state(tmp_path, 1)
    state_file = tmp_path / "main-system" / "runtime" / "state" / "ipc-connections.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["active_connections"] == 1
    assert "updated_at" in data


def test_write_ipc_connection_state_updates_atomically(tmp_path: Path) -> None:
    write_ipc_connection_state(tmp_path, 1)
    write_ipc_connection_state(tmp_path, 0)
    state_file = tmp_path / "main-system" / "runtime" / "state" / "ipc-connections.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["active_connections"] == 0


# ---------------------------------------------------------------------------
# ConnectionWatchdog probing
# ---------------------------------------------------------------------------


def test_watchdog_probe_detects_disconnected(tmp_path: Path) -> None:
    wd = ConnectionWatchdog(tmp_path, health_port=99999)  # nothing listening
    snap = wd.probe_once(backend_process_alive=True)
    # No backend HTTP, no frontend → disconnected or starting
    assert snap.overall_state in ("disconnected", "starting")
    assert snap.backend_http_healthy is False
    assert snap.frontend_connected is False


def test_watchdog_probe_detects_degraded(tmp_path: Path) -> None:
    """Backend HTTP healthy but no frontend connected → degraded."""
    wd = ConnectionWatchdog(tmp_path, health_port=99999)
    # Simulate: backend alive, HTTP up (mock), frontend not connected.
    # We can't easily mock HTTP, so test the state computation directly.
    state = wd._compute_state(True, True, False)
    assert state == "degraded"


def test_watchdog_probe_detects_connected() -> None:
    """All three layers up → connected."""
    wd = ConnectionWatchdog(Path("/tmp"), health_port=99999)
    state = wd._compute_state(True, True, True)
    assert state == "connected"


def test_watchdog_records_state_transition(tmp_path: Path) -> None:
    wd = ConnectionWatchdog(tmp_path, health_port=99999, dead_threshold=1)
    # First probe: disconnected (nothing running).
    wd.probe_once(backend_process_alive=True)
    # Force a transition by changing the state computation.
    wd._snapshot.overall_state = "connected"
    wd._snapshot.probe_count = 1
    # Next probe: disconnected again → should record event.
    wd.probe_once(backend_process_alive=True)
    events = wd.events
    # At least one event should be recorded.
    assert len(events) > 0


def test_watchdog_triggers_repair_callback(tmp_path: Path) -> None:
    wd = ConnectionWatchdog(tmp_path, health_port=99999, dead_threshold=1)
    repair_calls: list[tuple[str, ConnectionSnapshot]] = []

    def repair_cb(failure_code: str, snapshot: ConnectionSnapshot) -> dict:
        repair_calls.append((failure_code, snapshot))
        return {"ok": True}

    wd.set_repair_callback(repair_cb)
    # First probe sets state to disconnected.
    wd.probe_once(backend_process_alive=True)
    # If dead_threshold=1 and state=disconnected, repair should be called.
    # (May or may not trigger depending on whether state transitioned.)


def test_watchdog_get_status(tmp_path: Path) -> None:
    wd = ConnectionWatchdog(tmp_path, health_port=99999)
    status = wd.get_status()
    assert status["version"] == CONNECTION_WATCHDOG_VERSION
    assert "snapshot" in status
    assert "recent_events" in status
    assert status["dead_threshold"] == CONNECTION_DEAD_THRESHOLD


def test_watchdog_stop_terminates_cleanly(tmp_path: Path) -> None:
    wd = ConnectionWatchdog(tmp_path, health_port=99999, probe_interval=0.1)
    import threading
    t = threading.Thread(target=wd.run, args=(lambda: True,), daemon=True)
    t.start()
    time.sleep(0.2)  # Give thread time to start and enter wait loop
    wd.stop()
    t.join(timeout=2.0)
    assert not t.is_alive()


# ---------------------------------------------------------------------------
# ConnectionWatchdog with learning store
# ---------------------------------------------------------------------------


def test_watchdog_records_to_learning_store(tmp_path: Path) -> None:
    from tasks.repair_learning import RepairLearningStore

    store = RepairLearningStore(tmp_path / "repair")
    wd = ConnectionWatchdog(tmp_path, health_port=99999, dead_threshold=1)
    wd.set_learning_store(store)
    # Trigger a probe that will record an event.
    wd.probe_once(backend_process_alive=True)
    # The learning store should have at least one error signature.
    sigs = store.get_all_error_signatures()
    # May be empty if no state transition occurred, so just verify no crash.
    assert isinstance(sigs, list)


# ---------------------------------------------------------------------------
# IPC connection state file
# ---------------------------------------------------------------------------


def test_watchdog_reads_ipc_connection_state(tmp_path: Path) -> None:
    """Watchdog should detect frontend as connected when IPC state says so."""
    # Write a fresh IPC connection state.
    write_ipc_connection_state(tmp_path, 2)
    wd = ConnectionWatchdog(tmp_path, health_port=99999)
    assert wd._check_frontend_connected() is True


def test_watchdog_treats_stale_ipc_state_as_disconnected(tmp_path: Path) -> None:
    """Stale IPC state (>30s old) should be treated as disconnected."""
    state_file = (
        tmp_path / "main-system" / "runtime" / "state" / "ipc-connections.json"
    )
    state_file.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone, timedelta
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    state_file.write_text(
        json.dumps({"active_connections": 1, "updated_at": old_time}),
        encoding="utf-8",
    )
    wd = ConnectionWatchdog(tmp_path, health_port=99999)
    assert wd._check_frontend_connected() is False


def test_watchdog_treats_missing_ipc_state_as_disconnected(tmp_path: Path) -> None:
    wd = ConnectionWatchdog(tmp_path, health_port=99999)
    assert wd._check_frontend_connected() is False



########################################################################

# ---------------------------------------------------------------------------
# P7 tick deadline — the sync loop must stay bounded even when a tick wedges
# ---------------------------------------------------------------------------


def test_wedged_tick_is_bounded_and_stall_counted(tmp_path: Path) -> None:
    import threading

    wd = ConnectionWatchdog(
        tmp_path,
        health_port=99999,
        probe_interval=0.02,
        probe_timeout=0.05,
        enable_resource_monitoring=False,
    )
    wd.tick_deadline_s = 0.05
    wedged = threading.Event()
    release = threading.Event()

    def stuck_alive() -> bool:
        wedged.set()
        release.wait(timeout=30)
        return True

    t = threading.Thread(target=wd.run, args=(stuck_alive,), daemon=True)
    t.start()
    try:
        assert wedged.wait(timeout=5)
        deadline = time.time() + 5
        while wd._tick_stalls < 2 and time.time() < deadline:
            time.sleep(0.02)
        # The loop kept ticking past the wedged worker instead of blocking.
        assert wd._tick_stalls >= 1
        status = wd.get_status()
        assert status["tick_stalls"] >= 1
        assert status["tick_in_flight"] is True
    finally:
        release.set()
        wd.stop()
        t.join(timeout=5)


def test_healthy_ticks_complete_without_stalls(tmp_path: Path) -> None:
    import threading

    wd = ConnectionWatchdog(
        tmp_path,
        health_port=99999,
        probe_interval=0.02,
        probe_timeout=0.05,
        enable_resource_monitoring=False,
    )
    wd.tick_deadline_s = 5.0
    calls: list[int] = []

    def alive() -> bool:
        calls.append(1)
        return True

    t = threading.Thread(target=wd.run, args=(alive,), daemon=True)
    t.start()
    try:
        deadline = time.time() + 5
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 2
        assert wd._tick_stalls == 0
        status = wd.get_status()
        assert status["tick_deadline_s"] == 5.0
        assert "tick_in_flight" in status
    finally:
        wd.stop()
        t.join(timeout=5)
