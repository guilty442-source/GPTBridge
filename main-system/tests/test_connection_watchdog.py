import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

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


def test_recovery_records_success_outcome_without_connected_error(
    tmp_path: Path,
) -> None:
    import sqlite3

    from tasks.repair_learning import RepairLearningStore

    watchdog = ConnectionWatchdog(tmp_path)
    store = RepairLearningStore(
        tmp_path / "main-system" / "data" / "automatic-repair"
    )
    watchdog.set_learning_store(store)

    snapshot = watchdog.snapshot
    watchdog._record_event("starting", "disconnected", snapshot)
    watchdog._record_event("disconnected", "connected", snapshot)

    db_path = (
        tmp_path
        / "main-system"
        / "data"
        / "automatic-repair"
        / "repair-learning.sqlite3"
    )
    with sqlite3.connect(db_path) as connection:
        failures = dict(
            connection.execute(
                "SELECT error_class, signature_hash FROM error_signatures"
            )
        )
        outcomes = list(
            connection.execute("SELECT signature_hash, ok FROM repair_outcomes")
        )

    assert set(failures) == {"FRONTEND_BACKEND_DISCONNECTED"}
    fault_hash = failures["FRONTEND_BACKEND_DISCONNECTED"]
    assert any(hash_ == fault_hash and ok == 0 for hash_, ok in outcomes)
    assert any(hash_ == fault_hash and ok == 1 for hash_, ok in outcomes)


def test_recovery_signature_is_never_promoted(tmp_path: Path) -> None:
    from tasks.repair_learning import (
        ErrorSignature,
        RepairLearner,
        RepairLearningStore,
        RepairOutcome,
        _normalize_error_signature,
    )

    store = RepairLearningStore(tmp_path / "repair")
    learner = RepairLearner(store)
    signature = ErrorSignature(
        signature_hash=_normalize_error_signature(
            "CONNECTION_CONNECTED", "starting->connected", file_path="ipc/connection"
        ),
        error_class="CONNECTION_CONNECTED",
        message_pattern="starting->connected",
        failure_code="CONNECTION_CONNECTED",
        file_context="ipc/connection",
        target_tool_id="main-system",
    )
    for index in range(3):
        learner.learn_from_outcome(
            signature,
            RepairOutcome(
                run_id=f"run-{index}",
                signature_hash=signature.signature_hash,
                remedy="connection-watchdog",
                ok=True,
            ),
        )

    assert learner.store.get_learned_recipes() == []


def test_low_success_rate_remedy_is_never_promoted(tmp_path: Path) -> None:
    from tasks.repair_learning import (
        ErrorSignature,
        RepairLearner,
        RepairLearningStore,
        RepairOutcome,
        _normalize_error_signature,
    )

    store = RepairLearningStore(tmp_path / "repair")
    learner = RepairLearner(store)
    signature = ErrorSignature(
        signature_hash=_normalize_error_signature(
            "CONNECTION_DEGRADED", "connected->degraded", file_path="ipc/connection"
        ),
        error_class="CONNECTION_DEGRADED",
        message_pattern="connected->degraded",
        failure_code="CONNECTION_DEGRADED",
        file_context="ipc/connection",
        target_tool_id="main-system",
    )
    for index, ok in enumerate((True, False, False)):
        learner.learn_from_outcome(
            signature,
            RepairOutcome(
                run_id=f"run-{index}",
                signature_hash=signature.signature_hash,
                remedy="connection-watchdog",
                ok=ok,
            ),
        )

    assert learner.store.get_learned_recipes() == []
