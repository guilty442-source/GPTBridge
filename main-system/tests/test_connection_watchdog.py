import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from tasks.connection_watchdog import ConnectionWatchdog


def test_persistent_unready_runtime_signals_once(tmp_path: Path) -> None:
    watchdog = ConnectionWatchdog(tmp_path, dead_threshold=3)
    watchdog._probe_backend_http = lambda: False
    watchdog._check_frontend_connected = lambda: True
    signals: list[str] = []
    watchdog.set_repair_callback(lambda code, _snapshot: signals.append(code))

    for _ in range(4):
        watchdog.probe_once(backend_process_alive=True)

    assert signals == ["FRONTEND_BACKEND_DISCONNECTED"]
    assert watchdog.snapshot.overall_state == "starting"


def test_recovery_rearms_persistent_fault_signal(tmp_path: Path) -> None:
    watchdog = ConnectionWatchdog(tmp_path, dead_threshold=1)
    health = iter((False, True, False))
    watchdog._probe_backend_http = lambda: next(health)
    watchdog._check_frontend_connected = lambda: True
    signals: list[str] = []
    watchdog.set_repair_callback(lambda code, _snapshot: signals.append(code))

    watchdog.probe_once(backend_process_alive=True)
    watchdog.probe_once(backend_process_alive=True)
    watchdog.probe_once(backend_process_alive=True)

    assert signals == [
        "FRONTEND_BACKEND_DISCONNECTED",
        "FRONTEND_BACKEND_DISCONNECTED",
    ]
