"""Sovereign child supervision and runtime-readiness regression tests (W2).

Covers the faults that killed the in-process autonomy loops:

* ``_all_children`` must exist on every supervising sovereign — without it
  ``_supervise_children`` raised ``AttributeError`` on every tick and the
  loop's later steps (process survival, convergence, metric persistence)
  never ran again for the whole process generation.
* the runtime-readiness projection is nested under ``snapshot`` and must
  be read relative to ``app.project_root`` — otherwise a healthy runtime
  was misread as ``unknown`` (degraded) forever.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src-core"))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT.parent / "shared-layer" / "src"))

from governance.sovereigns.automation_sovereign import (  # noqa: E402
    AutomationSovereign,
)
from governance.sovereigns.decision_sovereign import DecisionSovereign  # noqa: E402
from governance.sovereigns.system_runtime_sovereign import (  # noqa: E402
    SystemRuntimeSovereign,
)
from governance.sovereigns.xingcheng.learning_sub_sovereign import (  # noqa: E402
    LearningEvidenceSyncSubSovereign,
)
from governance.sovereigns.xingcheng_sovereign import XingchengSovereign  # noqa: E402

SOVEREIGN_CLASSES = (SystemRuntimeSovereign, DecisionSovereign, AutomationSovereign)


def _app(project_root: Path) -> SimpleNamespace:
    return SimpleNamespace(project_root=project_root)


@pytest.mark.parametrize("sovereign_cls", SOVEREIGN_CLASSES)
def test_all_children_is_available(sovereign_cls: type) -> None:
    assert callable(getattr(sovereign_cls, "_all_children", None))


@pytest.mark.parametrize("sovereign_cls", SOVEREIGN_CLASSES)
def test_supervise_children_does_not_raise(sovereign_cls: type, tmp_path: Path) -> None:
    sovereign = sovereign_cls(_app(tmp_path))
    asyncio.run(sovereign._supervise_children())
    assert isinstance(sovereign._all_children(), dict)


def test_supervise_children_records_stopped_child(tmp_path: Path) -> None:
    # A334: ``_all_children`` only projects children whose codex hierarchy
    # row is active — use the active pair (decision-sovereign /
    # health-maintenance-test-sub-sovereign); the former fixture
    # startup-sub-sovereign under system-runtime-sovereign is retired.
    sovereign = DecisionSovereign(_app(tmp_path))
    stopped = SimpleNamespace(_started=False)
    sovereign._sub_sovereigns["health-maintenance-test-sub-sovereign"] = stopped

    asyncio.run(sovereign._supervise_children())

    assert "health-maintenance-test-sub-sovereign" in sovereign._all_children()
    assert (
        sovereign._child_supervision["health-maintenance-test-sub-sovereign"]["state"]
        == "stopped"
    )


@pytest.mark.parametrize(
    "sovereign_cls",
    (SystemRuntimeSovereign, DecisionSovereign, AutomationSovereign),
)
def test_autonomy_tick_survives(
    sovereign_cls: type, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full tick must reach metric persistence instead of aborting."""
    sovereign = sovereign_cls(_app(tmp_path))
    persisted: list[bool] = []
    method_name = (
        "_persist_metrics"
        if sovereign_cls is SystemRuntimeSovereign
        else "_persist_live_state"
    )
    monkeypatch.setattr(
        sovereign, method_name, lambda: persisted.append(True)
    )

    asyncio.run(sovereign._autonomy_tick())

    assert persisted == [True]


def test_runtime_readiness_snapshot_schema_and_project_root(tmp_path: Path) -> None:
    sovereign = SystemRuntimeSovereign(_app(tmp_path))
    state_dir = tmp_path / "main-system" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "runtime-readiness.json").write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "snapshot": {
                    "runtime_state": "ready",
                    "overall_ready": True,
                    "dependencies_ready": True,
                },
                "updated_at": "2026-09-16T12:18:26+00:00",
            }
        ),
        encoding="utf-8",
    )

    asyncio.run(sovereign._check_runtime_readiness())

    assert sovereign._runtime_state == "ready"
    assert sovereign._auto_metrics["degradation_detected"] == 0
    assert sovereign._load_readiness_state()["runtime_state"] == "ready"


def test_runtime_readiness_legacy_flat_schema(tmp_path: Path) -> None:
    sovereign = SystemRuntimeSovereign(_app(tmp_path))
    state_dir = tmp_path / "main-system" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "runtime-readiness.json").write_text(
        json.dumps({"state": "serving"}), encoding="utf-8"
    )

    asyncio.run(sovereign._check_runtime_readiness())

    assert sovereign._runtime_state == "serving"
    assert sovereign._auto_metrics["degradation_detected"] == 0


def test_runtime_readiness_degraded_still_detected(tmp_path: Path) -> None:
    sovereign = SystemRuntimeSovereign(_app(tmp_path))
    state_dir = tmp_path / "main-system" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "runtime-readiness.json").write_text(
        json.dumps({"snapshot": {"runtime_state": "failed"}}), encoding="utf-8"
    )

    asyncio.run(sovereign._check_runtime_readiness())

    assert sovereign._runtime_state == "failed"
    assert sovereign._auto_metrics["degradation_detected"] == 1


@pytest.mark.asyncio
async def test_learning_automation_rearms_after_child_materializes(
    tmp_path: Path,
) -> None:
    """start_supervision runs before the children exist; the parent's loop
    must re-command learning until the child is actually armed (A485)."""
    app = SimpleNamespace(project_root=tmp_path)
    parent = XingchengSovereign(app)
    app.xingcheng_sovereign = parent
    child = LearningEvidenceSyncSubSovereign(app, parent=parent)
    parent._sub_sovereigns["learning-evidence-sync-sub-sovereign"] = child

    assert await parent.ensure_learning_automation() is False
    assert child._reconcile_task is None

    await child.start()
    try:
        assert await parent.ensure_learning_automation() is True
        assert parent._learning_armed is True
        assert child._reconcile_task is not None
    finally:
        await child.stop()
        parent._learning_armed = False
