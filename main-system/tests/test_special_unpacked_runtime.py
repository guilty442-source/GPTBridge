from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
LOCAL_MODEL_ROOT = ROOT / "local-model"
CORE = ROOT / "main-system" / "src-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from tasks.toolbox_service import ToolboxService  # noqa: E402


class GovernanceStub:
    def __init__(self) -> None:
        self.authorized_lifecycle: list[tuple[str, str]] = []
        self.bootstrap_tool_ids: list[str] = []

    def authorize_tool_lifecycle(self, _tool_id: str, _action: str) -> None:
        self.authorized_lifecycle.append((_tool_id, _action))
        return None

    def create_tool_governance_bootstrap(self, _tool_id: str) -> str:
        self.bootstrap_tool_ids.append(_tool_id)
        return "governed-bootstrap"

    def can_start_tool(self, _tool_id: str) -> bool:
        return True


def test_special_unpacked_manifest_resolves_governed_channel_entry() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads((LOCAL_MODEL_ROOT / "manifest.json").read_text("utf-8"))
    entry = service._resolve_special_unpacked_entry(manifest, LOCAL_MODEL_ROOT)
    assert entry == (LOCAL_MODEL_ROOT / "src" / "channel_runtime.py").resolve()
    record = service._manifest_to_record(LOCAL_MODEL_ROOT, manifest)
    assert record["runtime_mode"] == "governed-source"
    assert record["runtime_available"] is True
    assert record["executable_exists"] is False
    assert record["data_boundary"] == {
        "standalone": True,
        "code_scope": "project-source-excluding-governance-rule",
            "database_scope": "opaque-central-index-read-and-xingcheng-internal-read-write",
    }


def test_model_dialogue_is_discovered_as_xingcheng_companion_tool() -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)
    records = {record["id"]: record for record in service._load_manifest_records()}

    assert not (ROOT / "star-chat").exists()
    assert records["xingcheng"]["folder_path"] == str(LOCAL_MODEL_ROOT)
    assert records["star-chat"]["folder_path"] == str(
        LOCAL_MODEL_ROOT / "model-dialogue"
    )
    assert records["star-chat"]["runtime_available"] is True
    assert service._tool_directory_for_id("star-chat") == (
        LOCAL_MODEL_ROOT / "model-dialogue"
    ).resolve()
    assert records["star-chat"]["runtime_owner_tool_id"] == "xingcheng"
    assert records["star-chat"]["physical_owner_root"] == "local-model"
    service._authorize_tool_lifecycle("star-chat", "start")
    assert governance.authorized_lifecycle[-1] == ("xingcheng", "start")


def test_shared_layer_and_local_model_are_locked_resident_services() -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)
    records = {record["id"]: record for record in service._load_manifest_records()}

    shared_manifest = json.loads(
        (ROOT / "shared-layer" / "manifest.json").read_text("utf-8")
    )
    local_manifest = json.loads(
        (ROOT / "local-model" / "manifest.json").read_text("utf-8")
    )
    assert records["shared-layer"]["runtime_available"] is True
    assert shared_manifest["main_system_independent_tool"] is True
    assert local_manifest["main_system_independent_tool"] is True
    assert shared_manifest["lifecycle"]["stoppable"] is False
    assert local_manifest["lifecycle"]["stoppable"] is False
    assert shared_manifest["background_service"]["auto_restart"] is True
    assert local_manifest["background_service"]["auto_restart"] is True


@pytest.mark.parametrize("tool_id", ["governance_rule", "shared-layer", "xingcheng"])
def test_locked_service_rejects_stop_before_governance_or_process_mutation(
    tool_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)
    stop_calls: list[Path] = []
    monkeypatch.setattr(
        service,
        "_stop_running_source_runtime",
        lambda entry: stop_calls.append(entry) or [43210],
    )

    result = asyncio.run(
        service.force_close_tool(
            {"tool_id": tool_id, "request_id": f"locked-stop-{tool_id}"}
        )
    )

    assert result == {
        "ok": False,
        "tool_id": tool_id,
        "request_id": f"locked-stop-{tool_id}",
        "error_code": "LIFECYCLE_LOCKED",
        "message": "LIFECYCLE_LOCKED",
    }
    assert governance.authorized_lifecycle == []
    assert stop_calls == []


@pytest.mark.asyncio
async def test_resident_services_are_usable_without_showing_permission_denied() -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)

    result = await service.list_tools()
    records = {record["id"]: record for record in result["tools"]}

    for tool_id in ("shared-layer", "xingcheng"):
        assert records[tool_id]["lifecycle_locked"] is True
        assert records[tool_id]["permission_denied"] is False
        assert records[tool_id]["resident_service"] is True


def test_companion_tool_cache_is_owned_by_host_tool() -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)
    tool_root = LOCAL_MODEL_ROOT / "model-dialogue"
    manifest = json.loads((tool_root / "manifest.json").read_text("utf-8"))

    environment = service._tool_environment("star-chat", tool_root, manifest)

    expected_cache = str(
        (LOCAL_MODEL_ROOT / "runtime" / "cache" / "companions" / "star-chat").resolve()
    )
    assert environment["GPTBRIDGE_TOOL_CACHE_ROOT"] == expected_cache
    assert environment["GPTBRIDGE_TOOL_TEMP_ROOT"] == str(
        (
            ROOT
            / "global-cleaner"
            / "runtime"
            / "temp"
            / "tools"
            / "star-chat"
        ).resolve()
    )
    assert environment["TEMP"] == environment["GPTBRIDGE_TOOL_TEMP_ROOT"]

    owner_environment = service._tool_environment(
        "star-chat",
        tool_root,
        manifest,
        governance_tool_id="xingcheng",
    )
    assert owner_environment["GPTBRIDGE_STANDALONE_TOOL_ID"] == "star-chat"
    assert governance.bootstrap_tool_ids[-1] == "xingcheng"

    mobile_root = ROOT / "investment-mobile"
    mobile_manifest = json.loads((mobile_root / "manifest.json").read_text("utf-8"))
    mobile_environment = service._tool_environment(
        "investment-mobile", mobile_root, mobile_manifest
    )
    assert mobile_environment["GPTBRIDGE_TOOL_CACHE_ROOT"] == str(
        (
            ROOT
            / "ai-assistant"
            / "runtime"
            / "cache"
            / "companions"
            / "investment-mobile"
        ).resolve()
    )


def test_ai_assistant_supports_automatic_dual_runtime() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads((ROOT / "ai-assistant" / "manifest.json").read_text("utf-8"))
    record = service._manifest_to_record(ROOT / "ai-assistant", manifest)

    assert manifest["launch"]["mode"] == "dual-runtime"
    assert manifest["launch"]["selection"] == "automatic"
    assert set(manifest["launch"]["runtimes"]) == {
        "governed-source-ui",
        "executable",
    }
    assert manifest["distribution"] == {"mode": "dual-runtime", "package": True}
    assert manifest["version"] == "1.0.0"
    assert manifest["display_version"] == "1.0"
    assert record["runtime_mode"] == "dual-runtime"
    assert record["automatic_runtime_mode"] == "governed-source"
    assert record["executable_exists"] is False
    assert record["data_boundary"]["database_scope"] == "tool-database-only"
    assert service._source_launch_requested(
        manifest,
        background=False,
        requested_mode="",
        executable_exists=True,
    ) is False
    assert service._source_launch_requested(
        manifest,
        background=True,
        requested_mode="",
        executable_exists=True,
    ) is True
    assert service._source_launch_requested(
        manifest,
        background=False,
        requested_mode="source",
        executable_exists=True,
    ) is True
    assert (ROOT / "main-system" / "scripts" / "source-tool-ui-host" / "main.cjs").is_file()


def test_independent_window_close_policy_covers_source_and_packaged_ui() -> None:
    toolbox_source = (
        ROOT / "main-system" / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text("utf-8")
    packaged_template = (
        ROOT
        / "main-system"
        / "src-core"
        / "tasks"
        / "templates"
        / "platform-tool-app"
        / "main.cjs"
    ).read_text("utf-8")

    assert "independent-tool-window-closed" in toolbox_source
    assert "close_program_on_exit" in toolbox_source
    assert "shutdownOwnedBackendBeforeExit" in packaged_template
    assert "backend.shutdownOnWindowClose" in packaged_template
    assert "app.on('before-quit'" in packaged_template


def test_foreground_ui_exit_force_closes_the_complete_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 31415
        returncode = 0

        async def wait(self) -> int:
            return 0

    service = ToolboxService(ROOT, governance=GovernanceStub())
    process = ExitedProcess()
    service._request_tool_ids["window-test"] = "ai-assistant"
    service._request_kinds["window-test"] = "started"
    service._running_processes["window-test"] = process  # type: ignore[assignment]
    closed: list[dict[str, object]] = []

    async def fake_force_close(payload: dict[str, object]) -> dict[str, object]:
        closed.append(payload)
        service._force_closed_tool_ids.add(str(payload["tool_id"]))
        return {"ok": True, "force_closed": True}

    async def fake_status(_tool_id: str, _status: str) -> dict[str, object]:
        return {"ok": True}

    monkeypatch.setattr(service, "force_close_tool", fake_force_close)
    monkeypatch.setattr(service, "update_status", fake_status)

    asyncio.run(
        service._watch_started_tool(
            "window-test",
            "ai-assistant",
            ROOT / "ai-assistant" / "dist" / "ai-assistant.exe",
            False,
            process,  # type: ignore[arg-type]
            close_program_on_exit=True,
        )
    )

    assert len(closed) == 1
    assert closed[0]["tool_id"] == "ai-assistant"
    assert closed[0]["reason"] == "independent-tool-window-closed"


def test_automatic_repair_is_centralized_in_main_system() -> None:
    for manifest_path in sorted(ROOT.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text("utf-8"))
        if manifest.get("enabled", True) is False:
            continue
        assert manifest.get("version") == "1.0.0", manifest_path
        capabilities = manifest.get("capabilities")
        assert isinstance(capabilities, dict), manifest_path
        assert "auto-repair" not in capabilities, manifest_path
        assert "central-automatic-repair" not in capabilities, manifest_path
        assert not (manifest_path.parent / "src" / "auto_repair.py").exists()
    toolbox_source = (
        ROOT / "main-system" / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text("utf-8")
    assert "def _rebuild_tool_executable" not in toolbox_source
    assert "_request_central_repair" in toolbox_source
    assert "_request_system_rescue_repair" not in toolbox_source


def test_dual_runtime_ai_assistant_is_available_to_package_scope() -> None:
    tasks_root = ROOT / "main-system" / "src-core" / "tasks"
    if str(tasks_root) not in sys.path:
        sys.path.insert(0, str(tasks_root))
    from platform_packager import (
        iter_tools,
        validate_tool_version_baseline,
    )

    explicitly_selected = {tool_id for tool_id, _, _ in iter_tools({"ai-assistant"})}
    normal_all_scope = {tool_id for tool_id, _, _ in iter_tools(None)}
    assert "ai-assistant" in explicitly_selected
    assert "ai-assistant" in normal_all_scope
    invalid_manifest = {"version": "2.0.0", "display_version": "2.0"}
    version_check = validate_tool_version_baseline("ai-assistant", invalid_manifest)
    assert version_check["ok"] is False
    assert version_check["error_code"] == "TOOL_VERSION_MISMATCH"


def test_main_system_blocks_tool_version_mismatch_before_launch() -> None:
    failure = ToolboxService._tool_version_failure(
        "ai-assistant",
        "version-check",
        {"version": "2.0.0", "display_version": "2.0"},
    )
    assert failure is not None
    assert failure["error_code"] == "TOOL_VERSION_MISMATCH"
    assert ToolboxService._tool_version_failure(
        "ai-assistant",
        "version-check",
        {"version": "1.0.0", "display_version": "1.0"},
    ) is None


def test_start_failure_requests_central_repair_then_retries_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads((ROOT / "ai-assistant" / "manifest.json").read_text("utf-8"))
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
            ROOT / "ai-assistant",
            manifest,
            {"error_code": "PACKAGE_UNVERIFIED"},
        )
    )

    assert result["ok"] is True
    assert calls == ["central-repair", "retry"]
    assert result["package_repair"]["owner"] == "main-system"


def test_star_is_headless_and_configured_for_governed_default_start() -> None:
    manifest = json.loads((LOCAL_MODEL_ROOT / "manifest.json").read_text("utf-8"))
    main_source = (ROOT / "main-system" / "src-core" / "main.py").read_text("utf-8")

    assert manifest["has_custom_ui"] is False
    assert "window" not in manifest
    assert "executable" not in manifest
    assert not (LOCAL_MODEL_ROOT / "src" / "ui").exists()
    assert manifest["background_service"]["auto_restart"] is True
    assert manifest["background_service"]["headless"] is True
    assert manifest["background_service"][
        "explicit_force_close_suppresses_restart"
    ] is True
    assert 'DEFAULT_START_TOOL_IDS = ("shared-layer", "xingcheng")' in main_source
    assert "await self._start_governed_default_tools()" in main_source


def test_file_sorter_does_not_start_a_full_electron_ui_in_background() -> None:
    manifest = json.loads((ROOT / "file-sorter" / "manifest.json").read_text("utf-8"))

    assert manifest["startup"]["auto_start"] is False
    assert manifest["automation"]["enabled"] is True
    assert manifest["has_custom_ui"] is True


def test_file_sorter_uses_governed_source_without_a_packaged_executable() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads((ROOT / "file-sorter" / "manifest.json").read_text("utf-8"))

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
    manifest = json.loads((LOCAL_MODEL_ROOT / "manifest.json").read_text("utf-8"))
    manifest["request_channel"]["model"] = "direct"
    with pytest.raises(ValueError, match="governance"):
        service._resolve_special_unpacked_entry(manifest, LOCAL_MODEL_ROOT)


def test_source_runtime_environment_has_ephemeral_authenticated_ipc() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads((LOCAL_MODEL_ROOT / "manifest.json").read_text("utf-8"))
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
    contract = json.loads(contract_path.read_text("utf-8"))
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
    assert result["runtime_path"].endswith("local-model\\src\\channel_runtime.py")


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
