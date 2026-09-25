from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
SHARED_SRC = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
sys.path.insert(0, str(SRC_CORE))
sys.path.insert(0, str(SHARED_SRC))

from core_system.daily_global_cleaner_service import DailyGlobalCleanerService
from core_system.main_system_self_maintenance import MainSystemSelfMaintenance


@pytest.mark.asyncio
async def test_automatic_cleanup_runs_in_process_without_spawn(tmp_path: Path) -> None:
    class Permission:
        def tool_execution_response(
            self, tool_id: str, request_id: str
        ) -> dict[str, object]:
            raise AssertionError("retired tool execution must never be requested")

    app = SimpleNamespace(
        project_root=tmp_path,
        permission_sovereign=Permission(),
        toolbox_service=None,
    )
    service = DailyGlobalCleanerService(app)

    async def cleanup_sweep(byte_budget: int | None = None) -> dict[str, object]:
        return {"ok": True, "module_count": 1, "cleaned_bytes_total": 0}

    service._run_module_self_cleanup_sweep = cleanup_sweep
    service._trash_candidates = lambda: []
    service._record_cleanup_audit = lambda outcome: "unavailable"
    result = await service.run_if_due(force=True)

    assert result["ok"] is True
    assert result["stage"] == "completed"
    assert result["request_id"].startswith("automatic-cleanup-")
    status = service.status()
    assert status["owner"] == "decision-sovereign"
    assert status["executor"] == "main-system-internal-cleanup"
    assert "global-cleaner" not in json.dumps(status)
    assert service.module_cleanup_status()["module_count"] == 1
    findings = result["detail"]["findings"]
    assert findings["quarantine"]["ok"] is True
    assert findings["orphans"]["removal_authorized"] is False


@pytest.mark.asyncio
async def test_automatic_cleanup_is_fail_open_and_records_degraded(
    tmp_path: Path,
) -> None:
    app = SimpleNamespace(
        project_root=tmp_path, permission_sovereign=None, toolbox_service=None
    )
    service = DailyGlobalCleanerService(app)
    phase_runs: list[str] = []

    async def trash_cleanup(byte_budget: int) -> dict[str, object]:
        phase_runs.append("trash")
        return {"ok": True, "cleaned_bytes": 0}

    async def failing_sweep(byte_budget: int | None = None) -> dict[str, object]:
        phase_runs.append("modules")
        raise RuntimeError("sweep exploded")

    service._cleanup_retired_trash = trash_cleanup
    service._run_module_self_cleanup_sweep = failing_sweep
    service._record_cleanup_audit = lambda outcome: "unavailable"

    result = await service.run_if_due(force=True)

    assert result["ok"] is False
    assert phase_runs == ["trash", "modules"]
    state = json.loads(service.state_path.read_text(encoding="utf-8"))
    assert state["last_ok"] is False
    assert state["last_error"]["error_code"] == "AUTOMATIC_CLEANUP_PARTIAL"
    assert state["last_outcome"]["findings"]["modules"]["ok"] is False


def test_trash_candidates_revalidate_the_current_registry(tmp_path: Path) -> None:
    registry_dir = (
        tmp_path / "governance_rule" / "execution" / "audit"
    )
    registry_dir.mkdir(parents=True)
    (registry_dir / "architecture_registry.json").write_text(
        json.dumps(
            {
                "module_labels": {"trash": ["global-cleaner", "not-retired"]},
                "components": [
                    {
                        "component_id": "global-cleaner",
                        "lifecycle": "retired",
                        "physical_path": "Standalone tools/global-cleaner",
                    },
                    {
                        "component_id": "not-retired",
                        "lifecycle": "on-demand",
                        "physical_path": "Standalone tools/not-retired",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "main-system" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "trash-cleanup-schedule.json").write_text(
        json.dumps(
            {
                "status": "scheduled",
                "candidate_ids": ["global-cleaner"],
                "revalidation_required": True,
            }
        ),
        encoding="utf-8",
    )
    app = SimpleNamespace(project_root=tmp_path)
    service = DailyGlobalCleanerService(app)

    candidates = service._trash_candidates()

    assert [item["component_id"] for item in candidates] == ["global-cleaner"]
    assert candidates[0]["physical_path"] == "Standalone tools/global-cleaner"
    assert candidates[0]["deletion_authorized"] is False

    # A schedule that lists no longer-retired ids yields no candidate.
    (state_dir / "trash-cleanup-schedule.json").write_text(
        json.dumps({"candidate_ids": ["not-retired"]}), encoding="utf-8"
    )
    assert service._trash_candidates() == []


def test_run_local_cleanup_enforces_byte_quota(tmp_path: Path) -> None:
    from governance_rule.execution.tool_runtime.tool_local_cleanup import (
        run_local_cleanup,
    )

    tool_root = tmp_path / "tool"
    temp_root = tool_root / "runtime" / "temp"
    temp_root.mkdir(parents=True)
    for index in range(4):
        (temp_root / f"blob-{index}.tmp").write_bytes(b"x" * 64)
    (temp_root / "keep").mkdir()

    result = run_local_cleanup("tool", tool_root, max_cleaned_bytes=128)

    assert result["ok"] is True
    assert result["cleaned_bytes"] <= 128
    assert any(
        str(item.get("reason") or "") == "cleanup byte quota reached"
        for item in result["skipped"]
    )
    # The contents-only anchor directory survives the sweep.
    assert temp_root.is_dir()


@pytest.mark.asyncio
async def test_main_system_cleanup_has_one_scheduler(tmp_path: Path) -> None:
    service = MainSystemSelfMaintenance(tmp_path)

    async def successful_duty() -> dict[str, object]:
        return {"ok": True}

    service._duty_source_self_repair = successful_duty
    service._duty_integrity_verify = successful_duty
    report = await service.run_once()

    assert not hasattr(service, "_duty_local_cleanup")
    assert report["duties"]["local_cleanup"] == {
        "ok": True,
        "skipped": True,
        "reason": "DAILY_GLOBAL_CLEANER_OWNS_SCHEDULE",
        "delegated_to": "daily-global-cleaner",
    }


def test_cleanup_run_source_has_no_retired_tool_spawn() -> None:
    run_source = (
        Path(__file__).resolve().parents[1]
        / "src-core"
        / "core_system"
        / "daily_global_cleaner_service_run.py"
    ).read_text(encoding="utf-8")
    facade_source = (
        Path(__file__).resolve().parents[1]
        / "src-core"
        / "core_system"
        / "daily_global_cleaner_service.py"
    ).read_text(encoding="utf-8")
    for marker in (
        '"tool_id": "global-cleaner"',
        "start_tool",
        "request_tool_execution",
        "force_close_tool",
    ):
        assert marker not in run_source
    assert '"executor": "global-cleaner"' not in facade_source


@pytest.mark.asyncio
async def test_tool_surface_excludes_the_retired_tool() -> None:
    from tasks.toolbox_service import ToolboxService

    class GovernanceStub:
        def authorize_tool_lifecycle(self, _tool_id: str, _action: str) -> None:
            return None

        def create_tool_governance_bootstrap(self, _tool_id: str) -> str:
            return "governed-bootstrap"

        def can_start_tool(self, _tool_id: str) -> bool:
            return True

    service = ToolboxService(
        Path(__file__).resolve().parents[2], governance=GovernanceStub()
    )
    listing = await service.list_tools()
    tool_ids = {str(record.get("id") or "") for record in listing["tools"]}

    assert "global-cleaner" not in tool_ids
    assert {"ai-assistant", "file-sorter", "vaultly"} <= tool_ids


@pytest.mark.asyncio
async def test_module_sweep_only_addresses_independent_tool_cards(
    tmp_path: Path,
) -> None:
    class Permission:
        def tool_execution_response(
            self, tool_id: str, request_id: str
        ) -> dict[str, object]:
            raise AssertionError(f"infrastructure commanded: {tool_id}")

    class Toolbox:
        async def list_tools(self) -> dict[str, object]:
            return {
                "ok": True,
                "tools": [
                    {
                        "id": "ai-assistant",
                        "enabled": True,
                        "status": "stopped",
                        "main_system_independent_tool": True,
                    },
                    {
                        "id": "shared-layer",
                        "enabled": True,
                        "status": "running",
                        "resident_service": True,
                        "main_system_independent_tool": False,
                    },
                    {
                        "id": "xingcheng",
                        "enabled": True,
                        "status": "running",
                        "resident_service": True,
                        "companion_tool": True,
                        "main_system_independent_tool": True,
                    },
                    {
                        "id": "star-chat",
                        "enabled": True,
                        "status": "running",
                        "companion_tool": True,
                        "hidden_from_toolbox": True,
                        "main_system_independent_tool": True,
                    },
                    {
                        "id": "system-rescue",
                        "enabled": True,
                        "status": "running",
                        "hidden_from_toolbox": True,
                        "main_system_independent_tool": True,
                    },
                    {
                        "id": "global-cleaner",
                        "enabled": False,
                        "status": "stopped",
                        "lifecycle": {"status": "retired"},
                    },
                ],
            }

        def _tool_directory_for_id(self, tool_id: str) -> Path:
            return tmp_path / "Standalone tools" / tool_id

    app = SimpleNamespace(
        project_root=tmp_path,
        toolbox_service=Toolbox(),
        permission_sovereign=Permission(),
        decision_sovereign=None,
    )
    service = DailyGlobalCleanerService(app)

    report = await service._run_module_self_cleanup_sweep(None)

    module_ids = {str(item.get("module_id") or "") for item in report["modules"]}
    assert module_ids == {"main-system", "ai-assistant"}
    assert report["infrastructure_skipped_count"] == 4
    assert service._is_sweepable_module(
        {"main_system_independent_tool": True}
    )
    assert not service._is_sweepable_module(
        {"main_system_independent_tool": True, "resident_service": True}
    )
    assert not service._is_sweepable_module(
        {"main_system_independent_tool": True, "hidden_from_toolbox": True}
    )


def test_orphan_root_classification_marks_garbage_and_review(
    tmp_path: Path,
) -> None:
    from tasks.repair_inspection import classify_orphan_component_roots

    registry_dir = tmp_path / "governance_rule" / "execution" / "audit"
    registry_dir.mkdir(parents=True)
    (registry_dir / "architecture_registry.json").write_text(
        json.dumps(
            {
                "components": [
                    {
                        "component_id": "local-model",
                        "physical_path": "Standalone tools/local-model",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    tools_root = tmp_path / "Standalone tools"
    (tools_root / "local-model").mkdir(parents=True)
    (tools_root / "shared-layer" / "config").mkdir(parents=True)
    (tools_root / "vaultly").mkdir()
    (tools_root / "vaultly" / "residue.bin").write_bytes(b"x")
    (tools_root / "system-rescue").mkdir()
    (tools_root / "system-rescue" / "manifest.json").write_text(
        "{}", encoding="utf-8"
    )

    candidates = classify_orphan_component_roots(tmp_path)
    by_id = {str(item["component_id"]): item for item in candidates}

    assert "local-model" not in by_id
    assert by_id["shared-layer"]["label"] == "garbage"
    assert by_id["shared-layer"]["empty"] is True
    assert by_id["vaultly"]["label"] == "review"
    assert by_id["vaultly"]["empty"] is False
    assert by_id["system-rescue"]["label"] == "review"
    assert by_id["system-rescue"]["has_manifest"] is True
    assert all(item["removal_authorized"] is False for item in candidates)


def test_crash_quarantine_retention_drops_only_aged_records(
    tmp_path: Path,
) -> None:
    from core_system.tool_isolation import ToolIsolationManager

    quarantine = (
        tmp_path / "main-system" / "runtime" / "state" / "tool-crash-quarantine"
    )
    quarantine.mkdir(parents=True)
    aged = quarantine / "global-cleaner-1.json"
    fresh = quarantine / "file-sorter-2.json"
    aged.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    old_epoch = time.time() - 30 * 24 * 60 * 60
    os.utime(aged, (old_epoch, old_epoch))

    manager = ToolIsolationManager(tmp_path)
    result = manager.purge_stale_quarantine(max_age_days=14)

    assert result["ok"] is True
    assert result["removed"] == ["global-cleaner-1.json"]
    assert fresh.is_file()
    assert not aged.exists()


@pytest.mark.asyncio
async def test_rag_generation_cleanup_skips_when_rag_not_started(
    tmp_path: Path,
) -> None:
    """Lazy contract: cleanup must never wake the RAG runtime."""
    app = SimpleNamespace(
        project_root=tmp_path,
        permission_sovereign=None,
        toolbox_service=None,
        rag_runtime=None,
    )
    service = DailyGlobalCleanerService(app)
    result = await service._cleanup_rag_generations()
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "rag-not-started"


@pytest.mark.asyncio
async def test_rag_generation_cleanup_dispatches_to_pipeline_loop(
    tmp_path: Path,
) -> None:
    import asyncio
    import concurrent.futures

    class FakeWorker:
        def submit(self, coro):
            future = concurrent.futures.Future()

            async def runner() -> None:
                try:
                    future.set_result(await coro)
                except Exception as error:  # pragma: no cover
                    future.set_exception(error)

            asyncio.get_running_loop().create_task(runner())
            return future

    class FakeManager:
        async def cleanup_old_generations(self):
            return SimpleNamespace(
                to_dict=lambda: {
                    "status": "OK",
                    "deleted": ["gen-20260901-aaa"],
                    "retained": ["gen-20260920-bbb"],
                }
            )

    rag_runtime = SimpleNamespace(
        _pipeline=SimpleNamespace(generation_manager=FakeManager()),
        _loop_worker=FakeWorker(),
    )
    app = SimpleNamespace(
        project_root=tmp_path,
        permission_sovereign=None,
        toolbox_service=None,
        rag_runtime=rag_runtime,
    )
    service = DailyGlobalCleanerService(app)
    result = await service._cleanup_rag_generations()
    assert result["ok"] is True
    assert result["deleted"] == ["gen-20260901-aaa"]
    assert result["operation"] == "rag-generation-cleanup"
