"""Split from consolidated test_main_system.py (main-system/tests/test_special_unpacked_runtime.py)."""
from __future__ import annotations

import _main_system_test_support as _support  # noqa: F401
from _main_system_test_support import ROOT, _read_text_cached, _parse_python_cached, LOCAL_MODEL_ROOT
from _test_special_unpacked_runtime_helpers import GovernanceStub

import asyncio
import json
import sys
from pathlib import Path
import pytest
from tasks.toolbox_service import ToolboxService  # noqa: E402

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
    integration_source = (
        ROOT
        / "main-system"
        / "src-core"
        / "core_system"
        / "integration_sub_sovereign.py"
    ).read_text("utf-8")

    assert manifest["has_custom_ui"] is False
    assert "window" not in manifest
    assert "executable" not in manifest
    assert not (LOCAL_MODEL_ROOT / "src" / "ui").exists()
    assert manifest["background_service"]["auto_restart"] is True
    assert manifest["background_service"]["headless"] is True
    assert manifest["background_service"][
        "explicit_force_close_suppresses_restart"
    ] is True
    # Default tool IDs and auto-start logic now live in the Integration
    # Sub-Sovereign (cross-module interface authority), not in main.py.
    # Only resident services (lifecycle.stoppable == false) are auto-started;
    # non-resident services start on demand.
    assert "_FALLBACK_RESIDENT_TOOL_IDS" in integration_source
    assert "self._start_governed_default_tools()" in integration_source
    assert "_classify_tools_by_manifest" in integration_source


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
            / "Standalone tools"
            / "global-cleaner"
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
    service._source_runtime_environments["xingcheng"] = {
        "GPTBRIDGE_IPC_PORT": "43210",
        "GPTBRIDGE_IPC_SESSION_TOKEN": "a" * 64,
    }
    service._source_ui_processes["star-chat"] = RunningProcess()  # type: ignore[assignment]
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

    assert reconnects == [("star-chat", "xingcheng")]


def test_source_ui_runtime_session_change_is_part_of_auto_repair() -> None:
    source = (
        ROOT / "main-system" / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text("utf-8")

    assert "_source_ui_runtime_sessions" in source
    assert "await self._reconnect_companion_source_uis(tool_id)" in source
    assert "orphaned_ui_ids" in source


def test_force_close_verifies_no_background_process_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    stop_calls: list[Path] = []
    monkeypatch.setattr(
        service,
        "_running_source_runtime_process_ids",
        lambda _entry: [],
    )
    monkeypatch.setattr(
        service,
        "_stop_running_source_runtime",
        lambda entry: stop_calls.append(entry) or [43210],
    )
    monkeypatch.setattr(service, "_running_executable_process_ids", lambda _entry: [])
    monkeypatch.setattr(service, "_stop_running_executable", lambda _entry: [])
    monkeypatch.setattr(service, "_running_packaged_backend_process_ids", lambda _root: [])
    monkeypatch.setattr(service, "_stop_running_packaged_backend", lambda _root: [])
    monkeypatch.setattr(service, "_running_source_ui_process_ids", lambda _tool_id: [])
    monkeypatch.setattr(service, "_stop_running_source_ui", lambda _tool_id: [])

    result = asyncio.run(
        service.force_close_tool(
            {
                "tool_id": "ai-assistant",
                "request_id": "force-close-test",
            }
        )
    )

    assert result["ok"] is True
    assert result["request_id"] == "force-close-test"
    assert result["force_closed"] is True
    assert result["remaining_process_ids"] == []
    assert result["force_closed_process_ids"] == [43210]
    assert len(stop_calls) == 2
    assert "ai-assistant" in service._force_closed_tool_ids


def test_force_close_fails_if_a_background_process_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    monkeypatch.setattr(
        service,
        "_running_source_runtime_process_ids",
        lambda _entry: [99999],
    )
    monkeypatch.setattr(
        service,
        "_stop_running_source_runtime",
        lambda _entry: [99999],
    )
    monkeypatch.setattr(service, "_running_executable_process_ids", lambda _entry: [])
    monkeypatch.setattr(service, "_stop_running_executable", lambda _entry: [])
    monkeypatch.setattr(service, "_running_packaged_backend_process_ids", lambda _root: [])
    monkeypatch.setattr(service, "_stop_running_packaged_backend", lambda _root: [])
    monkeypatch.setattr(service, "_running_source_ui_process_ids", lambda _tool_id: [])
    monkeypatch.setattr(service, "_stop_running_source_ui", lambda _tool_id: [])

    result = asyncio.run(
        service.force_close_tool(
            {
                "tool_id": "ai-assistant",
                "request_id": "force-close-survivor-test",
            }
        )
    )

    assert result["ok"] is False
    assert result["error_code"] == "FORCE_CLOSE_FAILED"
    assert result["remaining_process_ids"] == [99999]


def test_force_close_hybrid_tool_stops_exe_source_backend_and_ui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    monkeypatch.setattr(service, "_stop_running_source_runtime", lambda _entry: [1001])
    monkeypatch.setattr(service, "_stop_running_executable", lambda _entry: [1002])
    monkeypatch.setattr(service, "_stop_running_packaged_backend", lambda _root: [1003])
    monkeypatch.setattr(service, "_stop_running_source_ui", lambda _tool_id: [1004])
    monkeypatch.setattr(service, "_running_source_runtime_process_ids", lambda _entry: [])
    monkeypatch.setattr(service, "_running_executable_process_ids", lambda _entry: [])
    monkeypatch.setattr(service, "_running_packaged_backend_process_ids", lambda _root: [])
    monkeypatch.setattr(service, "_running_source_ui_process_ids", lambda _tool_id: [])

    result = asyncio.run(
        service.force_close_tool(
            {"tool_id": "ai-assistant", "request_id": "hybrid-force-close"}
        )
    )

    assert result["ok"] is True
    assert result["force_closed_process_ids"] == [1001, 1002, 1003, 1004]


def test_force_close_includes_orphaned_packaged_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    monkeypatch.setattr(service, "_running_executable_process_ids", lambda _entry: [])
    monkeypatch.setattr(service, "_stop_running_executable", lambda _entry: [])
    monkeypatch.setattr(service, "_running_source_runtime_process_ids", lambda _entry: [])
    monkeypatch.setattr(service, "_stop_running_source_runtime", lambda _entry: [])
    monkeypatch.setattr(service, "_running_source_ui_process_ids", lambda _tool_id: [])
    monkeypatch.setattr(service, "_stop_running_source_ui", lambda _tool_id: [])
    monkeypatch.setattr(
        service,
        "_stop_running_packaged_backend",
        lambda _tool_dir: [24680],
    )
    monkeypatch.setattr(
        service,
        "_running_packaged_backend_process_ids",
        lambda _tool_dir: [],
    )

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
