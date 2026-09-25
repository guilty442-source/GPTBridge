"""Split from consolidated test_main_system.py (main-system/tests/test_special_unpacked_runtime.py)."""
from __future__ import annotations

import _main_system_test_support as _support  # noqa: F401
from _main_system_test_support import ROOT, _read_text_cached, _parse_python_cached, LOCAL_MODEL_ROOT
from _test_special_unpacked_runtime_helpers import GovernanceStub

import asyncio
import json
import tempfile
import sys
from pathlib import Path
import pytest
from tasks.toolbox_service import ToolboxService  # noqa: E402

def _create_test_tool_dir(tool_id: str, stoppable: bool = True, independent: bool = False) -> Path:
    """Create a temporary tool directory with a manifest for testing."""
    tmpdir = Path(tempfile.mkdtemp(prefix=f"test_tool_{tool_id}_"))
    manifest = {
        "id": tool_id,
        "status": "stopped",
        "enabled": True,
        "lifecycle": {"stoppable": stoppable},
        "main_system_independent_tool": independent,
        "has_custom_ui": False,
        "runtime": {"type": "python", "entry": "src/main.py"},
        "distribution": {"mode": "special-unpackaged", "package": False},
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "runtime_entry": "src/channel_runtime.py",
            "direct_instruction": "PERMISSION_DENIED"
        },
        "launch": {"background": "governed-source-channel"},
    }
    (tmpdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # Create dummy runtime entry file
    (tmpdir / "src").mkdir(exist_ok=True)
    (tmpdir / "src" / "channel_runtime.py").write_text("# dummy", encoding="utf-8")
    return tmpdir

@pytest.fixture
def temp_test_tool():
    """Create a temporary test tool directory that is stoppable and not independent."""
    tmpdir = _create_test_tool_dir("test-force-close-tool", stoppable=True, independent=False)
    yield tmpdir
    # Cleanup
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

def test_start_failure_requests_central_repair_then_retries_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads(_read_text_cached(str((ROOT / "Standalone tools" / "ai-assistant" / "manifest.json"))))
    calls: list[str] = []

    async def fake_central_repair(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("central-repair")
        return {
            "triggered": True,
            "ok": True,
            "authority": "main-system",
            "channel": "integrated-central-repair",
            "detail": {
                "ok": True,
                "operation": "central-automatic-repair",
                "executed_actions": ["rebuild-tool-executable"],
                "package_repair": {
                    "triggered": True,
                    "ok": True,
                    "owner": "main-system",
                },
            },
            "backup_extraction": None,
        }

    async def fake_retry(_payload: dict[str, object]) -> dict[str, object]:
        calls.append("retry")
        return {"ok": True, "runtime_mode": "executable"}

    monkeypatch.setattr(service, "_request_central_repair", fake_central_repair)
    monkeypatch.setattr(service, "start_tool", fake_retry)
    result = asyncio.run(
        service._retry_start_after_central_repair(
            {"tool_id": "ai-assistant", "request_id": "repair-test"},
            "ai-assistant",
            ROOT / "Standalone tools" / "ai-assistant",
            manifest,
            {"error_code": "PACKAGE_UNVERIFIED"},
        )
    )

    assert result["ok"] is True
    assert calls == ["central-repair", "retry"]
    assert result["package_repair"]["owner"] == "main-system"


def test_star_is_headless_and_configured_for_governed_default_start() -> None:
    manifest = json.loads(_read_text_cached(str((LOCAL_MODEL_ROOT / "manifest.json"))))

    assert manifest["has_custom_ui"] is False
    assert "window" not in manifest
    assert "executable" not in manifest
    assert not (LOCAL_MODEL_ROOT / "src" / "ui").exists()
    # Default-off: the local model is stoppable (non-resident) and never
    # auto-restarted; it starts only on demand.
    assert manifest["lifecycle"]["stoppable"] is True
    assert manifest["background_service"]["auto_restart"] is False
    assert manifest["background_service"]["headless"] is True
    assert manifest["background_service"][
        "explicit_force_close_suppresses_restart"
    ] is True
    # A604: tool-start classification moved off the retired integration
    # sub-sovereign — activation policy is declared per manifest
    # (§10.7 eager/on-demand); local-model is strictly on-demand.
    assert manifest["lifecycle"]["startup"] == "on-demand"


def test_file_sorter_does_not_start_a_full_electron_ui_in_background() -> None:
    manifest = json.loads(_read_text_cached(str((ROOT / "Standalone tools" / "file-sorter" / "manifest.json"))))

    assert manifest["startup"]["auto_start"] is False
    assert manifest["automation"]["enabled"] is True
    assert manifest["has_custom_ui"] is True


def test_file_sorter_uses_governed_source_without_a_packaged_executable() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads(_read_text_cached(str((ROOT / "Standalone tools" / "file-sorter" / "manifest.json"))))

    assert manifest["distribution"] == {
        "mode": "special-unpackaged",
        "package": False,
    }
    assert manifest["launch"] == {
        "primary": "governed-source-channel",
        "background": "governed-source-channel",
        "close_program_on_window_exit": True,
    }
    assert "executable" not in manifest
    assert "package" not in manifest
    assert service._source_launch_requested(
        manifest,
        background=False,
        requested_mode="",
        executable_exists=True,
    ) is True


def test_governed_source_ui_host_exposes_authenticated_backend_session() -> None:
    host_source = (
        ROOT / "main-system" / "scripts" / "source-tool-ui-host" / "main.cjs"
    ).read_text("utf-8")

    assert "token: String(backendSessionUrl?.searchParams.get('token') || '')" in host_source
    assert "/^[a-f0-9]{64}$/i.test(token)" in host_source
    assert "/^[a-f0-9]{24}$/i.test(instance)" in host_source
    assert "GPTBRIDGE_TOOL_CACHE_ROOT" in host_source
    assert "app.setPath('userData', userDataRoot)" in host_source
    assert "app.setPath('sessionData', sessionDataRoot)" in host_source
    assert "disk-cache-dir" in host_source


def test_special_unpacked_mode_requires_governed_request_channel() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads(_read_text_cached(str((LOCAL_MODEL_ROOT / "manifest.json"))))
    manifest["request_channel"]["model"] = "direct"
    with pytest.raises(ValueError, match="governance"):
        service._resolve_special_unpacked_entry(manifest, LOCAL_MODEL_ROOT)


def test_source_runtime_environment_has_ephemeral_authenticated_ipc() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads(_read_text_cached(str((LOCAL_MODEL_ROOT / "manifest.json"))))
    environment = service._source_runtime_environment(
        "xingcheng",
        LOCAL_MODEL_ROOT,
        manifest,
    )
    assert environment["GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP"] == "governed-bootstrap"
    assert len(environment["GPTBRIDGE_IPC_SESSION_TOKEN"]) == 64
    assert len(environment["GPTBRIDGE_SHUTDOWN_TOKEN"]) == 64
    assert 1024 <= int(environment["GPTBRIDGE_IPC_PORT"]) <= 65535
    expected_temp = str(
        (
            ROOT
            / "main-system"
            / "runtime"
            / "temp"
            / "tools"
            / "xingcheng"
        ).resolve()
    )
    assert environment["GPTBRIDGE_TOOL_TEMP_ROOT"] == expected_temp
    assert environment["TEMP"] == expected_temp
    assert environment["TMP"] == expected_temp
    assert environment["TMPDIR"] == expected_temp


def test_main_system_uses_the_governed_runtime_contract_location() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    contract_path = (
        service.project_root
        / "main-system"
        / "config"
        / "tool-runtime-contract.json"
    )
    contract = json.loads(_read_text_cached(str(contract_path)))
    assert contract["contract_version"] == 1
    assert contract["minimum_supported_contract_version"] == 1

    architecture = json.loads(
        (
            service.project_root
            / "main-system"
            / "config"
            / "data-architecture-contract.json"
        ).read_text("utf-8")
    )
    exclusions = architecture["git_policy"]["version_normalization_exclusions"]
    assert "local-model-runtime-and-model-release-versions" in exclusions
    assert "third-party-package-and-dependency-versions" in exclusions


def test_start_tool_does_not_require_exe_for_special_unpacked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    monkeypatch.setattr(
        service,
        "_running_source_runtime_process_ids",
        lambda _entry: [43210],
    )
    result = asyncio.run(
        service.start_tool(
            {
                "tool_id": "xingcheng",
                "request_id": "test-special-unpacked",
                "background": True,
            }
        )
    )
    assert result["ok"] is True
    assert result["runtime_mode"] == "governed-source"
    assert result["pid"] == 43210
    assert result["runtime_path"].endswith("xingcheng\\src\\channel_runtime.py")


def test_source_ui_waits_for_governed_runtime_health() -> None:
    source = (
        ROOT / "main-system" / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text("utf-8")
    assert 'f"http://127.0.0.1:{runtime_port}/health"' in source
    assert '"SOURCE_RUNTIME_NOT_READY"' in source
    assert 'payload.get("governance_ready") is True' in source


def test_owner_runtime_restart_reconnects_open_companion_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningProcess:
        returncode = None

    service = ToolboxService(ROOT, governance=GovernanceStub())
    service._source_runtime_environments["star-chat"] = {
        "GPTBRIDGE_IPC_PORT": "43210",
        "GPTBRIDGE_IPC_SESSION_TOKEN": "a" * 64,
    }
    service._source_ui_processes["model-dialogue"] = RunningProcess()  # type: ignore[assignment]
    reconnects: list[tuple[str, str]] = []

    async def fake_launch(
        tool_id: str,
        _tool_dir: Path,
        _manifest: dict[str, object],
        _runtime_environment: dict[str, str],
        *,
        runtime_tool_id: str | None = None,
    ) -> dict[str, object]:
        reconnects.append((tool_id, str(runtime_tool_id)))
        return {"ok": True}

    monkeypatch.setattr(service, "_launch_source_ui", fake_launch)

    asyncio.run(service._reconnect_companion_source_uis("xingcheng"))

    # No startable UI tool delegates its runtime to xingcheng any more:
    # model-dialogue runs its own lightweight governed runtime.
    assert reconnects == []


def test_source_ui_runtime_session_change_is_part_of_auto_repair() -> None:
    source = (
        ROOT / "main-system" / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text("utf-8")

    assert "_source_ui_runtime_sessions" in source
    assert "await self._reconnect_companion_source_uis(tool_id)" in source
    assert "orphaned_ui_ids" in source


def test_force_close_verifies_no_background_process_remains(
    monkeypatch: pytest.MonkeyPatch,
    temp_test_tool: Path,
) -> None:
    governance = GovernanceStub()
    governance.maintenance_ready = True
    service = ToolboxService(ROOT, governance=governance)
    stop_calls: list[Path] = []

    # Mock the tool directory and manifest loading
    monkeypatch.setattr(service, "_tool_directory_for_id", lambda tool_id: temp_test_tool)
    monkeypatch.setattr(service, "_load_manifest_cached", lambda tool_id: (
        {"lifecycle": {"stoppable": True}, "main_system_independent_tool": False}, temp_test_tool
    ))
    # Mock _run_bounded_sweep directly
    async def mock_run_bounded_sweep(*args, **kwargs):
        return set(), []
    monkeypatch.setattr(service, "_run_bounded_sweep", mock_run_bounded_sweep)
    # Also mock the terminate_tracked to return our stop calls
    async def mock_terminate(tool_id):
        stop_calls.append(temp_test_tool)
        return {43210}
    monkeypatch.setattr(service, "_terminate_tracked_tool_processes", mock_terminate)
    # Mock update_status
    async def mock_update_status(tool_id, status):
        return {"ok": True}
    monkeypatch.setattr(service, "update_status", mock_update_status)

    result = asyncio.run(
        service.force_close_tool(
            {
                "tool_id": "test-force-close-tool",
                "request_id": "force-close-test",
            }
        )
    )
    print(f"DEBUG result: {result}")

    assert result["ok"] is True
    assert result["request_id"] == "force-close-test"
    assert result["force_closed"] is True
    assert result["remaining_process_ids"] == []
    assert result["force_closed_process_ids"] == [43210]
    assert len(stop_calls) == 1
    assert result["within_budget"] is True
    assert result["budget_ms"] == 5000
    assert "test-force-close-tool" in service._force_closed_tool_ids


def test_force_close_fails_if_a_background_process_survives(
    monkeypatch: pytest.MonkeyPatch,
    temp_test_tool: Path,
) -> None:
    governance = GovernanceStub()
    governance.maintenance_ready = True
    service = ToolboxService(ROOT, governance=governance)

    # Mock the tool directory and manifest loading
    monkeypatch.setattr(service, "_tool_directory_for_id", lambda tool_id: temp_test_tool)
    monkeypatch.setattr(service, "_load_manifest_cached", lambda tool_id: (
        {"lifecycle": {"stoppable": True}, "main_system_independent_tool": False}, temp_test_tool
    ))
    # Mock _run_bounded_sweep to return remaining process
    async def mock_run_bounded_sweep_survivor(*args, **kwargs):
        return set(), [99999]
    monkeypatch.setattr(service, "_run_bounded_sweep", mock_run_bounded_sweep_survivor)
    async def mock_terminate(tool_id):
        return {99999}
    monkeypatch.setattr(service, "_terminate_tracked_tool_processes", mock_terminate)
    # Mock update_status
    async def mock_update_status(tool_id, status):
        return {"ok": True}
    monkeypatch.setattr(service, "update_status", mock_update_status)

    result = asyncio.run(
        service.force_close_tool(
            {
                "tool_id": "test-force-close-tool",
                "request_id": "force-close-survivor-test",
            }
        )
    )

    assert result["ok"] is False
    assert result["error_code"] == "FORCE_CLOSE_FAILED"
    assert result["remaining_process_ids"] == [99999]


def test_force_close_hybrid_tool_stops_exe_source_backend_and_ui(
    monkeypatch: pytest.MonkeyPatch,
    temp_test_tool: Path,
) -> None:
    governance = GovernanceStub()
    governance.maintenance_ready = True
    service = ToolboxService(ROOT, governance=governance)

    # Mock the tool directory and manifest loading
    monkeypatch.setattr(service, "_tool_directory_for_id", lambda tool_id: temp_test_tool)
    monkeypatch.setattr(service, "_load_manifest_cached", lambda tool_id: (
        {"lifecycle": {"stoppable": True}, "main_system_independent_tool": False}, temp_test_tool
    ))
    # Mock _run_bounded_sweep to return all stopped
    async def mock_run_bounded_sweep_all(*args, **kwargs):
        return {1001, 1002, 1003, 1004}, []
    monkeypatch.setattr(service, "_run_bounded_sweep", mock_run_bounded_sweep_all)
    async def mock_terminate(tool_id):
        return set()
    monkeypatch.setattr(service, "_terminate_tracked_tool_processes", mock_terminate)
    # Mock update_status
    async def mock_update_status(tool_id, status):
        return {"ok": True}
    monkeypatch.setattr(service, "update_status", mock_update_status)

    result = asyncio.run(
        service.force_close_tool(
            {"tool_id": "test-force-close-tool", "request_id": "hybrid-force-close"}
        )
    )

    assert result["ok"] is True
    assert result["force_closed_process_ids"] == [1001, 1002, 1003, 1004]


def test_force_close_includes_orphaned_packaged_backend(
    monkeypatch: pytest.MonkeyPatch,
    temp_test_tool: Path,
) -> None:
    governance = GovernanceStub()
    governance.maintenance_ready = True
    service = ToolboxService(ROOT, governance=governance)

    # Mock the tool directory and manifest loading
    monkeypatch.setattr(service, "_tool_directory_for_id", lambda tool_id: temp_test_tool)
    monkeypatch.setattr(service, "_load_manifest_cached", lambda tool_id: (
        {"lifecycle": {"stoppable": True}, "main_system_independent_tool": False}, temp_test_tool
    ))
    # Mock _run_bounded_sweep to return packaged backend stopped
    async def mock_run_bounded_sweep_packaged(*args, **kwargs):
        return {24680}, []
    monkeypatch.setattr(service, "_run_bounded_sweep", mock_run_bounded_sweep_packaged)
    async def mock_terminate(tool_id):
        return set()
    monkeypatch.setattr(service, "_terminate_tracked_tool_processes", mock_terminate)
    # Mock update_status
    async def mock_update_status(tool_id, status):
        return {"ok": True}
    monkeypatch.setattr(service, "update_status", mock_update_status)

    result = asyncio.run(
        service.force_close_tool(
            {
                "tool_id": "test-force-close-tool",
                "request_id": "force-close-packaged-backend-test",
            }
        )
    )

    assert result["ok"] is True
    assert result["force_closed_process_ids"] == [24680]

    result = asyncio.run(
        service.force_close_tool(
            {
                "tool_id": "file-sorter",
                "request_id": "force-close-packaged-backend-test",
            }
        )
    )

    assert result["ok"] is True
    assert result["force_closed_process_ids"] == [24680]
    assert result["remaining_process_ids"] == []



########################################################################