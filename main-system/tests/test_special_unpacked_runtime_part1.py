"""Split from consolidated test_main_system.py (main-system/tests/test_special_unpacked_runtime.py)."""
from __future__ import annotations

import _main_system_test_support as _support  # noqa: F401
from _main_system_test_support import ROOT, _read_text_cached, _parse_python_cached, LOCAL_MODEL_ROOT
from _test_special_unpacked_runtime_helpers import GovernanceStub

import asyncio
import json
import os
import sys
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "main-system" / "src-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


from tasks.toolbox_service import ToolboxService  # noqa: E402




def test_special_unpacked_manifest_resolves_governed_channel_entry() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads(_read_text_cached(str((LOCAL_MODEL_ROOT / "manifest.json"))))
    entry = service._resolve_special_unpacked_entry(manifest, LOCAL_MODEL_ROOT)
    assert entry == (LOCAL_MODEL_ROOT / "src" / "channel_runtime.py").resolve()
    record = service._manifest_to_record(LOCAL_MODEL_ROOT, manifest)
    assert record["runtime_mode"] == "governed-source"
    assert record["runtime_available"] is True
    assert record["executable_exists"] is False
    assert record["data_boundary"] == {
        "standalone": True,
        "code_scope": "tool-root-only",
        "database_scope": "tool-database-only",
    }


def test_local_model_companion_tools_are_discovered() -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)
    records = {record["id"]: record for record in service._load_manifest_records()}

    assert not (ROOT / "star-chat").exists()
    assert records["xingcheng"]["folder_path"] == str(
        LOCAL_MODEL_ROOT / "xingcheng"
    )
    assert records["model-dialogue"]["folder_path"] == str(
        LOCAL_MODEL_ROOT / "model-dialogue"
    )
    assert records["model-dialogue"]["runtime_available"] is True
    # star-chat is a companion component of model-dialogue, not an
    # independent tool: it is not discovered as a startable record.
    assert "star-chat" not in records
    assert service._tool_directory_for_id("model-dialogue") == (
        LOCAL_MODEL_ROOT / "model-dialogue"
    ).resolve()
    service._authorize_tool_lifecycle("model-dialogue", "start")
    assert governance.authorized_lifecycle[-1] == ("model-dialogue", "start")


def test_manifest_cache_ignores_other_worktree_path() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    stale_dir = (
        ROOT
        / ".kilo"
        / "worktrees"
        / "chinese-semantic-engine-core"
        / "Standalone tools"
        / "local-model"
        / "model-dialogue"
    )
    service._manifest_cache["model-dialogue"] = ({"id": "model-dialogue"}, stale_dir)
    service._manifest_cache_keys["model-dialogue"] = (0, 0)

    resolved = service._tool_directory_for_id("model-dialogue")

    assert resolved == (LOCAL_MODEL_ROOT / "model-dialogue").resolve()
    assert service._manifest_cache["model-dialogue"][1] == resolved


def test_manifest_cache_reloads_after_manifest_edit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    tool_dir = tmp_path / "cache-tool"
    tool_dir.mkdir()
    manifest_path = tool_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"id": "cache-tool", "lifecycle": {"stoppable": False}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        service, "_tool_directory_for_id", lambda _tool_id: tool_dir
    )

    first, _dir = service._load_manifest_cached("cache-tool")
    assert first["lifecycle"]["stoppable"] is False

    stat_result = manifest_path.stat()
    manifest_path.write_text(
        json.dumps({"id": "cache-tool", "lifecycle": {"stoppable": True}}),
        encoding="utf-8",
    )
    os.utime(
        manifest_path,
        ns=(
            stat_result.st_mtime_ns + 1_000_000_000,
            stat_result.st_mtime_ns + 1_000_000_000,
        ),
    )

    refreshed, _dir = service._load_manifest_cached("cache-tool")
    assert refreshed["lifecycle"]["stoppable"] is True


def test_shared_layer_and_local_model_are_locked_resident_services() -> None:
    governance = GovernanceStub()
    service = ToolboxService(ROOT, governance=governance)
    records = {record["id"]: record for record in service._load_manifest_records()}

    shared_manifest = json.loads(
        (ROOT / "shared-layer" / "manifest.json").read_text("utf-8")
    )
    local_manifest = json.loads(
        (ROOT / "Standalone tools" / "local-model" / "manifest.json").read_text("utf-8")
    )
    assert records["shared-layer"]["runtime_available"] is True
    assert shared_manifest["main_system_independent_tool"] is False
    assert local_manifest["main_system_independent_tool"] is True
    # shared-layer stays a locked resident service.
    assert shared_manifest["lifecycle"]["stoppable"] is False
    assert shared_manifest["background_service"]["auto_restart"] is True
    # The local model is default-off and closable: non-resident (not started
    # at boot) and stoppable, while the cleaner never commands it.
    assert local_manifest["lifecycle"]["stoppable"] is True
    assert local_manifest["background_service"]["auto_restart"] is False
    assert local_manifest["sweep_exclusion"] is True


@pytest.mark.parametrize("tool_id", ["governance_rule", "shared-layer"])
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
    dialogue_root = LOCAL_MODEL_ROOT / "model-dialogue"
    manifest = json.loads(_read_text_cached(str((dialogue_root / "manifest.json"))))

    environment = service._tool_environment("model-dialogue", dialogue_root, manifest)

    # model-dialogue is hosted under local-model: its cache lives under the
    # host's cache storage as a named companion compartment.
    assert environment["GPTBRIDGE_TOOL_CACHE_ROOT"] == str(
        (
            LOCAL_MODEL_ROOT / "runtime" / "cache" / "companions" / "model-dialogue"
        ).resolve()
    )
    assert environment["GPTBRIDGE_TOOL_TEMP_ROOT"] == str(
        (
            ROOT
            / "main-system"
            / "runtime"
            / "temp"
            / "tools"
            / "model-dialogue"
        ).resolve()
    )
    assert environment["TEMP"] == environment["GPTBRIDGE_TOOL_TEMP_ROOT"]

    # Identity resolution: model-dialogue is its own sealed runtime
    # identity; local-model's runtime claims the nested xingcheng identity.
    assert service._governed_runtime_tool_id("model-dialogue") == "model-dialogue"
    assert service._governed_runtime_tool_id("local-model") == "xingcheng"

    owner_environment = service._tool_environment(
        "model-dialogue",
        dialogue_root,
        manifest,
        governance_tool_id="model-dialogue",
    )
    assert owner_environment["GPTBRIDGE_STANDALONE_TOOL_ID"] == "model-dialogue"
    assert governance.bootstrap_tool_ids[-1] == "model-dialogue"

    mobile_root = ROOT / "Standalone tools" / "investment-mobile"
    mobile_manifest = json.loads(_read_text_cached(str((mobile_root / "manifest.json"))))
    mobile_environment = service._tool_environment(
        "investment-mobile", mobile_root, mobile_manifest
    )
    assert mobile_environment["GPTBRIDGE_TOOL_CACHE_ROOT"] == str(
        (
            ROOT
            / "Standalone tools"
            / "investment-mobile"
            / "runtime"
            / "cache"
            / "companions"
            / "investment-mobile"
        ).resolve()
    )


def test_ai_assistant_supports_automatic_dual_runtime() -> None:
    service = ToolboxService(ROOT, governance=GovernanceStub())
    manifest = json.loads(_read_text_cached(str((ROOT / "Standalone tools" / "ai-assistant" / "manifest.json"))))
    record = service._manifest_to_record(ROOT / "Standalone tools" / "ai-assistant", manifest)

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
        / "main.ts"
    ).read_text("utf-8")

    assert "independent-tool-window-closed" in toolbox_source
    assert "close_program_on_exit" in toolbox_source
    assert "shutdownOwnedBackendBeforeExit" in packaged_template
    assert "backend.shutdownOnWindowClose" in packaged_template
    assert "app.on('before-quit'" in packaged_template


def test_main_startup_follows_declared_dag_and_detaches_ui() -> None:
    phases_source = "\n".join(
        path.read_text("utf-8")
        for path in sorted(
            (ROOT / "main-system" / "src-core" / "startup_core").glob("phases*.py")
        )
    )
    boot_source = "\n".join(
        path.read_text("utf-8")
        for path in sorted(
            (ROOT / "main-system" / "src-core").glob("boot_core*.py")
        )
    )
    ui_source = (
        ROOT / "main-system" / "src-ui" / "main" / "index.ts"
    ).read_text("utf-8")

    assert "DEPENDENCY_MANIFEST" in phases_source
    assert "DependencyDeclaration(**entry)" in phases_source
    assert "ThreadPoolExecutor" in phases_source
    assert "STARTUP_GATE_DEADLINE_SECONDS: Final[float] = _cfg_probe" in phases_source
    assert "include_self_health=False" in phases_source
    assert "CrashRepair" not in boot_source
    boot_repair_source = (ROOT / "main-system" / "src-core" / "boot_core_repair.py").read_text("utf-8")
    assert "signal_only=True" in boot_repair_source
    before_quit = ui_source.split("app.on('before-quit', (event) =>", 1)[1]
    assert "shutdownApplication()" in before_quit
    assert "stopBackend()" in ui_source
    assert "main.ui-shutdown" in ui_source


def test_hot_reload_and_connection_recovery_are_generation_safe() -> None:
    root = ROOT / "main-system"
    watcher = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "tasks").glob("hot_reload_watcher*.py"))
    ) + "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "tasks" / "hot_reload_watcher").glob("*.py"))
    )
    update = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "core_system").glob("hot_update_service*.py"))
    )
    backend = (root / "src-ui" / "main" / "python-backend.ts").read_text(
        "utf-8"
    )
    boot = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core").glob("boot_core*.py"))
    )
    lifecycle = (root / "src-core" / "ipc" / "server_lifecycle.py").read_text(
        "utf-8"
    )
    notifier = (
        root / "src-core" / "tasks" / "state_change_notifier.py"
    ).read_text("utf-8")
    handler = (root / "src-core" / "ipc" / "server_handler.py").read_text(
        "utf-8"
    )
    socket = (
        root
        / "src-ui"
        / "renderer"
        / "shared"
        / "hooks"
        / "useBackendSocket.ts"
    ).read_text("utf-8")
    hmr = (
        root
        / "src-ui"
        / "renderer"
        / "shared"
        / "services"
        / "hmrService.ts"
    ).read_text("utf-8")

    assert "if await self._maybe_reload(changed):" in watcher
    assert "self._pending = {}" in watcher
    assert 'compile(source, str(file_path), "exec")' in update
    assert "module.__dict__.update(state)" in update
    assert "probeExistingBackend" in backend
    assert "requestGracefulBackendShutdown" in backend
    assert "GPTBRIDGE_SHUTDOWN_TOKEN" in backend
    boot_health = (root / "src-core" / "boot_core_health.py").read_text("utf-8")
    assert "if healthy:\n                    self._restarts = 0" in boot_health
    assert 'probe_port}/health?brief=1' in boot_health
    assert "BackendGateway(HEALTH_PROBE_PORT)" in boot
    assert 'query == "brief=1" or query == "level=brief"' in lifecycle
    assert "readiness = notifier.current_snapshot()" in lifecycle
    assert "await asyncio.to_thread(self._gate.evaluate)" in notifier
    assert "status_payload = snapshot.as_dict()" in handler
    assert "WS_STALE_CONNECTION_MS" in socket
    assert "QUEUE_ITEM_EXPIRED" in socket
    assert "runtime:hot-reload-completed" in hmr


def test_backend_gateway_and_watcher_use_atomic_ab_handover() -> None:
    root = ROOT / "main-system"
    gateway = (root / "src-core" / "backend_gateway.py").read_text("utf-8")
    boot = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core").glob("boot_core*.py"))
    )
    watcher = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "tasks").glob("hot_reload_watcher*.py"))
    ) + "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "tasks" / "hot_reload_watcher").glob("*.py"))
    )
    update = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "core_system").glob("hot_update_service*.py"))
    )
    handlers = (root / "src-core" / "ipc" / "handlers.py").read_text("utf-8")
    command_router = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core" / "ipc" / "command_router").glob("*.py"))
    )

    assert "class BackendGateway" in gateway
    assert "BACKEND_GENERATION_PORTS" in boot
    boot_handover = (root / "src-core" / "boot_core_handover.py").read_text("utf-8")
    boot_health = (root / "src-core" / "boot_core_health.py").read_text("utf-8")
    assert "self._gateway.activate(standby_port, generation)" in boot_handover
    assert "healthy and not self._probe_health(self._health_probe_port)" in boot_health
    assert "def is_running(self) -> bool:" in gateway
    assert "standby-readiness-failed" in boot_handover
    assert "backend-update-request.json" in watcher
    assert '"terminal_status": "prepared"' in watcher
    assert "os.replace(temporary, request_path)" in watcher
    assert "def prepare_generation(" in update
    assert "This performs no live-module mutation" in update
    assert "standby_validation=True" in watcher
    assert "await watcher._maybe_reload(changed_paths)" in command_router
    assert "runtime_sovereign.execute_hot_reload" not in handlers


def test_backend_entry_keeps_sovereign_runtime_imports_for_next_generation() -> None:
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        path.read_text("utf-8")
        for path in sorted((root / "src-core").glob("main*.py"))
    )
    for runtime_name in (
        "DecisionSovereign",
        "PermissionSovereign",
        "SystemRuntimeSovereign",
        "AutomationSovereign",
        "XingchengSovereign",
    ):
        assert runtime_name in source


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
            ROOT / "Standalone tools" / "ai-assistant" / "dist" / "ai-assistant.exe",
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
        manifest = json.loads(_read_text_cached(str(manifest_path)))
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
    invalid_manifest = {"version": "release-two", "display_version": "2.0"}
    version_check = validate_tool_version_baseline("ai-assistant", invalid_manifest)
    assert version_check["ok"] is False
    assert version_check["error_code"] == "TOOL_VERSION_MISMATCH"


def test_main_system_blocks_tool_version_mismatch_before_launch() -> None:
    failure = ToolboxService._tool_version_failure(
        "ai-assistant",
        "version-check",
        {"version": "2.0.0", "display_version": "1.0"},
    )
    assert failure is not None
    assert failure["error_code"] == "TOOL_VERSION_MISMATCH"
    assert ToolboxService._tool_version_failure(
        "ai-assistant",
        "version-check",
        {"version": "1.0.0", "display_version": "1.0"},
    ) is None
    assert ToolboxService._tool_version_failure(
        "ai-assistant",
        "version-check",
        {"version": "2.7.3", "display_version": "2.7"},
    ) is None
