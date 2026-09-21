"""§10.27 / §10.63 R5 — resident-core manifest and on-demand watcher tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

from startup_core.resident_core import (  # noqa: E402
    ON_DEMAND,
    RESIDENT_CORE,
    manifest_components,
    resident_mode,
)
from tasks.hot_reload_watcher import HotReloadWatcher  # noqa: E402


def test_manifest_classifies_hot_reload_on_demand() -> None:
    assert resident_mode("hot_reload_watcher") == ON_DEMAND


def test_manifest_classifies_minimal_core_resident() -> None:
    for component in (
        "ipc_server",
        "governance_gates",
        "runtime_state",
        "periodic_scheduler",
        "core_sovereigns",
    ):
        assert resident_mode(component) == RESIDENT_CORE, component


def test_unknown_component_defaults_resident() -> None:
    # Fail-safe: an unlisted/corrupt entry must not silently disable a
    # governed component — status quo is resident.
    assert resident_mode("no-such-component") == RESIDENT_CORE


def test_manifest_components_observable() -> None:
    components = manifest_components()
    assert "hot_reload_watcher" in components
    assert components["rag_cag"]["mode"] == ON_DEMAND


def test_mark_available_enables_reload_without_loop() -> None:
    app = SimpleNamespace(project_root=str(Path(__file__).resolve().parents[2]))
    watcher = HotReloadWatcher(app)
    assert watcher._enabled is False
    assert watcher._task is None

    watcher.mark_available()
    assert watcher._enabled is True
    assert watcher._task is None  # no poll loop spawned

    # The governed reload path (_maybe_reload → _preflight_checks) gates
    # on _enabled; with the loop off the capability must still answer.
    assert watcher._in_flight is False


def test_preflight_gate_respects_enabled_flag() -> None:
    app = SimpleNamespace(
        project_root=str(Path(__file__).resolve().parents[2]),
        startup_dead=False,
        maintenance_ready=True,
        automation_sovereign=object(),
        decision_sovereign=object(),
        governance=object(),
    )
    watcher = HotReloadWatcher(app)
    assert watcher._preflight_checks() is False  # not enabled → closed
    watcher.mark_available()
    assert watcher._preflight_checks() is True
