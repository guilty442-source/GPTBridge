"""§10.65 act-1 shadow wiring tests for connection_watchdog.

The harness runs the C ``NativeWatchdog`` in parallel while Python stays
authoritative; these tests cover flag parsing, parity silence, divergence
auditing, and fail-closed behaviour.  When the governed extension is not
built the native-dependent cases degrade to flag/paths checks only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from tasks.connection_watchdog import ConnectionWatchdog
from tasks.connection_watchdog_native_shadow import WatchdogNativeShadow


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"connection_watchdog": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_wd() -> object:
    from core_system.native import _sovereign_native as native

    return native.NativeWatchdog(1000, 60000, 3, 2)


def _native_available() -> bool:
    try:
        _native_wd()
    except Exception:
        return False
    return True


requires_native = pytest.mark.skipif(
    not _native_available(), reason="native pyd unavailable or predates E1 bindings"
)


def test_shadow_absent_without_policy(tmp_path: Path) -> None:
    watchdog = ConnectionWatchdog(tmp_path)
    assert watchdog._native_shadow is None


def test_shadow_absent_when_mode_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    watchdog = ConnectionWatchdog(tmp_path)
    assert watchdog._native_shadow is None


def test_primary_mode_refused_fail_closed(tmp_path: Path) -> None:
    # act-2 mode is not wired in act-1: refuse -> Python-only
    _write_policy(tmp_path, "primary")
    watchdog = ConnectionWatchdog(tmp_path)
    assert watchdog._native_shadow is None


def test_shadow_absent_on_invalid_policy(tmp_path: Path) -> None:
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{ not json", encoding="utf-8")
    watchdog = ConnectionWatchdog(tmp_path)
    assert watchdog._native_shadow is None


class _StubNative:
    """Fake native watchdog returning a fixed divergent outcome."""

    def __init__(self, state: str = "disconnected", dead: int = 99, repair: bool = True):
        self._state = state
        self._dead = dead
        self._repair = repair
        self.probed = 0

    def probe(self, *args):
        self.probed += 1
        return {"to_state": self._state, "repair_fired": self._repair}

    def state(self) -> str:
        return self._state

    def consecutive_dead(self) -> int:
        return self._dead

    def next_interval_ms(self) -> int:
        return 12345


class _RaisingNative:
    def probe(self, *args):
        raise RuntimeError("native boom")


def test_divergence_recorded_python_authoritative(tmp_path: Path) -> None:
    log = tmp_path / "main-system" / "runtime" / "logs" / "native-shadow" / "connection-watchdog.jsonl"
    shadow = WatchdogNativeShadow(log, _StubNative())
    shadow.observe_probe(
        backend_process_alive=True,
        backend_http_healthy=True,
        frontend_connected=True,
        py_state="connected",
        py_consecutive_dead=0,
        py_repair_trigger=False,
        now_ms=1000,
    )
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "divergence"
    assert rec["component"] == "connection_watchdog"
    assert rec["python"]["state"] == "connected"
    assert rec["native"]["state"] == "disconnected"
    assert set(rec["mismatches"]) == {"state", "consecutive_dead", "repair_trigger"}


def test_interval_divergence_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = WatchdogNativeShadow(log, _StubNative())
    shadow.observe_interval(45000)
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["kind"] == "interval-divergence"
    assert rec["python"]["interval_ms"] == 45000
    assert rec["native"]["interval_ms"] == 12345


def test_native_error_fail_closed_single_record(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = WatchdogNativeShadow(log, _RaisingNative())
    for _ in range(3):
        shadow.observe_probe(
            backend_process_alive=True,
            backend_http_healthy=True,
            frontend_connected=True,
            py_state="connected",
            py_consecutive_dead=0,
            py_repair_trigger=False,
            now_ms=1,
        )
        shadow.observe_interval(30000)
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["kind"] == "shadow-disabled"
    assert records[0]["reason"] == "native-probe-error"


def test_silent_match_writes_nothing(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    # stub matching python exactly -> no records
    shadow = WatchdogNativeShadow(log, _StubNative(state="connected", dead=0, repair=False))
    shadow.observe_probe(
        backend_process_alive=True,
        backend_http_healthy=True,
        frontend_connected=True,
        py_state="connected",
        py_consecutive_dead=0,
        py_repair_trigger=False,
        now_ms=1,
    )
    shadow.observe_interval(12345)
    assert not log.exists()


@requires_native
def test_real_native_shadow_stays_in_lockstep(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    watchdog = ConnectionWatchdog(tmp_path, probe_interval=1.0, dead_threshold=3)
    assert watchdog._native_shadow is not None
    watchdog._probe_backend_http = lambda: False
    watchdog._check_frontend_connected = lambda: True
    for _ in range(5):
        watchdog.probe_once(backend_process_alive=True)
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "connection-watchdog.jsonl"
    )
    if log.exists():
        divergent = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["kind"] == "divergence"
        ]
        assert divergent == []
    # Python path unaffected and still authoritative
    assert watchdog.snapshot.probe_count == 5
