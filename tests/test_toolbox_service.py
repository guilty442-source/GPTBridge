from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

import tasks.toolbox_service as toolbox_service_module
from tasks.toolbox_service import (
    MAX_TOOL_ARGUMENTS,
    MAX_TOOL_OUTPUT_CHARS,
    ToolboxService,
    _background_subprocess_kwargs,
)


def test_project_size_bytes_matches_disk_folder_total_without_exclusions(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "size-tool"
    (tool_dir / "src").mkdir(parents=True)
    (tool_dir / "dist").mkdir()
    (tool_dir / "build").mkdir()
    (tool_dir / "node_modules" / "pkg").mkdir(parents=True)
    (tool_dir / ".venv" / "Lib").mkdir(parents=True)
    (tool_dir / "__pycache__").mkdir()
    (tool_dir / ".pytest_cache").mkdir()
    (tool_dir / "src" / "main.py").write_bytes(b"a" * 10)
    (tool_dir / "dist" / "size-tool.exe").write_bytes(b"b" * 20)
    (tool_dir / "build" / "artifact.bin").write_bytes(b"c" * 30)
    (tool_dir / "node_modules" / "pkg" / "index.js").write_bytes(b"d" * 40)
    (tool_dir / ".venv" / "Lib" / "site.py").write_bytes(b"e" * 50)
    (tool_dir / "__pycache__" / "main.pyc").write_bytes(b"f" * 60)
    (tool_dir / ".pytest_cache" / "cache").write_bytes(b"g" * 70)

    assert ToolboxService._project_size_bytes(tool_dir) == 280


def test_background_subprocess_kwargs_hide_windows_console() -> None:
    kwargs = _background_subprocess_kwargs()

    if os.name == "nt" and getattr(subprocess, "CREATE_NO_WINDOW", 0):
        assert kwargs == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert kwargs == {}


@pytest.mark.asyncio
async def test_manifest_refresh_preserves_live_runtime_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir = tmp_path / "platform_tools" / "stable-tool"
    tool_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "stable-tool",
                "name": "Stable Tool",
                "version": "1.0.0",
                "status": "stopped",
                "enabled": True,
                "entry": "platform_tools/stable-tool/src/main",
                "executable": {"path": "dist/stable-tool.exe"},
            }
        ),
        encoding="utf-8",
    )
    service = ToolboxService(tmp_path)

    await service.list_tools()
    assert (await service.update_status("stable-tool", "running"))["ok"] is True
    monkeypatch.setattr(
        ToolboxService,
        "_running_executable_process_ids",
        staticmethod(lambda _executable_file: [12345]),
    )
    refreshed = await service.list_tools()

    tool = next(item for item in refreshed["tools"] if item["id"] == "stable-tool")
    assert tool["status"] == "running"


@pytest.mark.asyncio
async def test_standalone_toolbox_rejects_other_tool_ids(
    tmp_path: Path,
) -> None:
    service = ToolboxService(
        tmp_path,
        allowed_tool_ids={"file-sorter"},
    )

    result = await service.run_tool(
        {
            "tool_id": "project-cleaner",
            "request_id": "outside-scope",
        }
    )

    assert result["ok"] is False
    assert result["error_code"] == "TOOL_OUTSIDE_STANDALONE_SCOPE"


@pytest.mark.asyncio
async def test_start_tool_reuses_existing_executable_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir = tmp_path / "platform_tools" / "external-tool"
    dist_dir = tool_dir / "dist"
    dist_dir.mkdir(parents=True)
    exe_path = dist_dir / "external-tool.exe"
    exe_path.write_bytes(b"stub")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "external-tool",
                "name": "External Tool",
                "version": "1.0.0",
                "status": "stopped",
                "enabled": True,
                "entry": "platform_tools/external-tool/src/main",
                "executable": {"path": "dist/external-tool.exe"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ToolboxService,
        "_running_executable_process_ids",
        staticmethod(lambda _executable_file: [12345]),
    )
    monkeypatch.setattr(
        toolbox_service_module,
        "verify_packaged_app",
        lambda *_args, **_kwargs: {"ok": True},
    )

    captured: dict[str, object] = {}

    class FakeActivationProcess:
        pid = 54321
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args: object, **kwargs: object):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeActivationProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await ToolboxService(tmp_path).start_tool({"tool_id": "external-tool"})

    assert result["ok"] is True
    assert result["pid"] == 12345
    assert result["activated"] is True
    assert result["activation_pid"] == 54321
    assert result["message"] == "Tool executable is already running; activation requested"
    assert captured["args"] == (str(exe_path),)
    activation_env = captured["kwargs"]["env"]  # type: ignore[index]
    assert "ELECTRON_RUN_AS_NODE" not in activation_env
    assert "GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE" not in activation_env
    assert "GPTBRIDGE_MANAGED_BACKEND_TOOL_ID" not in activation_env

    captured.clear()
    background_result = await ToolboxService(tmp_path).start_tool(
        {"tool_id": "external-tool", "background": True}
    )

    assert background_result["ok"] is True
    assert background_result["background"] is True
    assert background_result["pid"] == 12345
    assert "activated" not in background_result
    assert captured == {}


@pytest.mark.asyncio
async def test_fast_secondary_exit_keeps_tool_running_when_primary_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ToolboxService(tmp_path)
    executable_file = tmp_path / "platform_tools" / "demo" / "dist" / "demo.exe"
    statuses: list[str] = []

    class FakeSecondaryProcess:
        async def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        ToolboxService,
        "_running_executable_process_ids",
        staticmethod(lambda _executable_file: [12345]),
    )

    async def capture_status(_tool_id: str, status: str) -> dict[str, object]:
        statuses.append(status)
        return {"ok": True}

    monkeypatch.setattr(service, "update_status", capture_status)

    await service._watch_started_tool(
        "secondary-request",
        "demo",
        executable_file,
        FakeSecondaryProcess(),  # type: ignore[arg-type]
    )

    assert statuses == ["running"]


@pytest.mark.asyncio
async def test_cancelled_started_process_cannot_restore_running_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ToolboxService(tmp_path)
    executable_file = tmp_path / "platform_tools" / "demo" / "dist" / "demo.exe"
    statuses: list[str] = []

    class FakeStoppedProcess:
        async def wait(self) -> int:
            return 0

    service._request_tool_ids["stopped-request"] = "demo"
    service._request_kinds["stopped-request"] = "started"
    service._cancelled_request_ids.add("stopped-request")
    monkeypatch.setattr(
        ToolboxService,
        "_running_executable_process_ids",
        staticmethod(lambda _executable_file: [12345]),
    )

    async def capture_status(_tool_id: str, status: str) -> dict[str, object]:
        statuses.append(status)
        return {"ok": True}

    monkeypatch.setattr(service, "update_status", capture_status)

    await service._watch_started_tool(
        "stopped-request",
        "demo",
        executable_file,
        FakeStoppedProcess(),  # type: ignore[arg-type]
    )

    assert statuses == ["stopped"]


@pytest.mark.asyncio
async def test_start_tool_verifies_package_before_reusing_existing_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir = tmp_path / "platform_tools" / "stale-tool"
    dist_dir = tool_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "stale-tool.exe").write_bytes(b"stub")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "stale-tool",
                "name": "Stale Tool",
                "version": "1.0.0",
                "enabled": True,
                "entry": "platform_tools/stale-tool/src/main",
                "executable": {"path": "dist/stale-tool.exe"},
            }
        ),
        encoding="utf-8",
    )
    process_probe_called = False

    def process_probe(_executable_file: Path) -> list[int]:
        nonlocal process_probe_called
        process_probe_called = True
        return [12345]

    monkeypatch.setattr(
        ToolboxService,
        "_running_executable_process_ids",
        staticmethod(process_probe),
    )
    monkeypatch.setattr(
        toolbox_service_module,
        "verify_packaged_app",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error_code": "STALE_PACKAGE",
            "message": "Source files changed after this EXE was packaged",
        },
    )

    result = await ToolboxService(tmp_path).start_tool(
        {"tool_id": "stale-tool"}
    )

    assert result["ok"] is False
    assert result["error_code"] == "STALE_PACKAGE"
    assert process_probe_called is False


@pytest.mark.asyncio
async def test_started_standalone_gui_does_not_block_its_tool_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir = tmp_path / "platform_tools" / "gui-tool"
    entry_dir = tool_dir / "src"
    dist_dir = tool_dir / "dist"
    entry_dir.mkdir(parents=True)
    dist_dir.mkdir()
    entry_file = entry_dir / "main.py"
    executable_file = dist_dir / "gui-tool.exe"
    entry_file.write_text("print('command completed')\n", encoding="utf-8")
    executable_file.write_bytes(b"stub")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "gui-tool",
                "name": "GUI Tool",
                "version": "1.0.0",
                "enabled": True,
                "entry": "platform_tools/gui-tool/src/main",
                "executable": {"path": "dist/gui-tool.exe"},
            }
        ),
        encoding="utf-8",
    )

    process_finished = asyncio.Event()

    class FakeStartedProcess:
        pid = 12345
        returncode: int | None = None

        async def wait(self) -> int:
            await process_finished.wait()
            return int(self.returncode or 0)

    started_process = FakeStartedProcess()
    service = ToolboxService(tmp_path)
    monkeypatch.setattr(
        ToolboxService,
        "_running_executable_process_ids",
        staticmethod(lambda _executable_file: []),
    )
    monkeypatch.setattr(
        toolbox_service_module,
        "verify_packaged_app",
        lambda *_args, **_kwargs: {"ok": True},
    )

    started_kwargs: dict[str, object] = {}

    async def fake_create_subprocess_exec(*_args: object, **kwargs: object):
        started_kwargs.update(kwargs)
        return started_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    start_result = await service.start_tool(
        {"tool_id": "gui-tool", "request_id": "gui-start"}
    )

    assert start_result["ok"] is True
    assert "gui-tool" not in service._active_request_by_tool
    assert service._started_request_by_tool["gui-tool"] == "gui-start"
    assert service._running_processes["gui-start"] is started_process
    started_env = started_kwargs["env"]
    assert isinstance(started_env, dict)
    assert "GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE" not in started_env

    async def fake_run_tool_streaming(**kwargs: object) -> dict[str, object]:
        await service._release_tool_process(str(kwargs["request_id"]))
        return {
            "exit_code": 0,
            "stdout": "command completed\n",
            "stderr": "",
            "stdout_encoding": "utf-8",
            "stderr_encoding": "utf-8",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "cancelled": False,
            "timed_out": False,
        }

    monkeypatch.setattr(service, "_run_tool_streaming", fake_run_tool_streaming)
    command_result = await service.run_tool(
        {"tool_id": "gui-tool", "request_id": "gui-command", "args": []}
    )

    assert command_result["ok"] is True
    assert command_result["stdout"] == "command completed\n"
    assert service._running_processes["gui-start"] is started_process

    started_process.returncode = 0
    process_finished.set()
    for _ in range(20):
        if "gui-start" not in service._running_processes:
            break
        await asyncio.sleep(0)
    assert "gui-start" not in service._running_processes
    assert "gui-tool" not in service._started_request_by_tool


@pytest.mark.asyncio
async def test_missing_tool_actions_remove_stale_tool_record(tmp_path: Path) -> None:
    service = ToolboxService(tmp_path)
    stale_tool_id = "tool-mqi8uv5x-fo9f"
    stale_tool_dir = tmp_path / "platform_tools" / stale_tool_id

    def insert_stale_record() -> None:
        service.repository.upsert_tool(
            {
                "id": stale_tool_id,
                "name": "投資看盤",
                "version": "2.0.0",
                "status": "stopped",
                "enabled": True,
                "entry": f"platform_tools/{stale_tool_id}/src/main",
                "manifest_path": str(stale_tool_dir / "manifest.json"),
                "code_path": str(stale_tool_dir / "src" / "main.py"),
            }
        )

    for method_name, payload in [
        ("start_tool", {"tool_id": stale_tool_id}),
        ("run_tool", {"tool_id": stale_tool_id}),
    ]:
        insert_stale_record()
        result = await getattr(service, method_name)(payload)

        assert result["ok"] is False
        assert result["removed"] is True
        assert result["tool_id"] == stale_tool_id
        assert stale_tool_id not in {tool["id"] for tool in service.repository.list_tools()}
        assert not stale_tool_dir.exists()

    insert_stale_record()
    stop_result = await service.stop_tool({"tool_id": stale_tool_id})

    assert stop_result["ok"] is True
    assert stop_result["removed"] is True
    assert stale_tool_id not in {tool["id"] for tool in service.repository.list_tools()}
    assert not stale_tool_dir.exists()


@pytest.mark.asyncio
async def test_stop_tool_stops_external_executable_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir = tmp_path / "platform_tools" / "external-tool"
    dist_dir = tool_dir / "dist"
    dist_dir.mkdir(parents=True)
    exe_path = dist_dir / "external-tool.exe"
    exe_path.write_bytes(b"stub")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "external-tool",
                "name": "External Tool",
                "version": "1.0.0",
                "status": "running",
                "enabled": True,
                "entry": "platform_tools/external-tool/src/main",
                "executable": {"path": "dist/external-tool.exe"},
            }
        ),
        encoding="utf-8",
    )
    stopped_paths: list[Path] = []

    def fake_stop(executable_file: Path) -> list[int]:
        stopped_paths.append(executable_file)
        return [12345]

    monkeypatch.setattr(
        ToolboxService,
        "_stop_running_executable",
        staticmethod(fake_stop),
    )

    result = await ToolboxService(tmp_path).stop_tool({"tool_id": "external-tool"})

    assert result["ok"] is True
    assert stopped_paths == [exe_path.resolve()]


@pytest.mark.asyncio
async def test_cancel_streaming_tool_run_stops_process(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "slow-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "slow-tool",
                "name": "Slow Tool",
                "version": "1.0.0",
                "status": "stopped",
                "enabled": True,
                "entry": "platform_tools/slow-tool/src/main",
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (entry_dir / "main.py").write_text(
        "\n".join(
            [
                "import time",
                "print('started', flush=True)",
                "time.sleep(30)",
                "print('finished', flush=True)",
            ]
        ),
        encoding="utf-8",
    )

    service = ToolboxService(tmp_path)

    async def event_callback(_event: str, _payload: dict) -> None:
        return None

    run_task = asyncio.create_task(
        service.run_tool(
            {"tool_id": "slow-tool", "request_id": "slow-request", "args": []},
            event_callback=event_callback,
        )
    )
    for _ in range(40):
        if "slow-request" in service._running_processes:
            break
        await asyncio.sleep(0.05)

    cancel_result = await service.cancel_tool_run(
        {"tool_id": "slow-tool", "request_id": "slow-request"}
    )
    run_result = await asyncio.wait_for(run_task, timeout=5)

    assert cancel_result["ok"] is True
    assert cancel_result["request_id"] == "slow-request"
    assert run_result["ok"] is False
    assert run_result["cancelled"] is True
    assert run_result["request_id"] == "slow-request"
    assert run_result["message"] == "Tool cancelled by user"
    assert "started" in run_result["stdout"]
    assert "slow-request" not in service._running_processes


@pytest.mark.parametrize(
    ("tool_id", "progress_prefix"),
    [
        ("investment-tool", "INVESTMENT_MANAGER_PROGRESS_JSON="),
        ("project-cleaner", "PROJECT_CLEANER_PROGRESS_JSON="),
    ],
)
@pytest.mark.asyncio
async def test_streaming_tool_run_forwards_progress(
    tmp_path: Path,
    tool_id: str,
    progress_prefix: str,
) -> None:
    tool_dir = tmp_path / "platform_tools" / tool_id
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": tool_id,
                "name": "Investment Tool",
                "version": "1.0.0",
                "status": "stopped",
                "enabled": True,
                "entry": f"platform_tools/{tool_id}/src/main",
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (entry_dir / "main.py").write_text(
        "\n".join(
            [
                "import json",
                "payload = {'phase': 'quote_snapshot', 'message': 'done'}",
                f"print('{progress_prefix}' + json.dumps(payload), flush=True)",
                "print('finished', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    service = ToolboxService(tmp_path)
    events: list[tuple[str, dict]] = []

    async def event_callback(event: str, payload: dict) -> None:
        events.append((event, payload))

    result = await service.run_tool(
        {"tool_id": tool_id, "request_id": "progress-request", "args": []},
        event_callback=event_callback,
    )

    assert result["ok"] is True
    assert "finished" in result["stdout"]
    assert events == [
        (
            "toolbox_run_tool_progress",
            {
                "ok": True,
                "tool_id": tool_id,
                "phase": "quote_snapshot",
                "message": "done",
                "request_id": "progress-request",
            },
        )
    ]


@pytest.mark.asyncio
async def test_run_tool_preserves_more_than_twenty_arguments_and_rejects_explicit_limit(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "platform_tools" / "args-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "args-tool",
                "name": "Args Tool",
                "enabled": True,
                "entry": "platform_tools/args-tool/src/main",
            }
        ),
        encoding="utf-8",
    )
    (entry_dir / "main.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    service = ToolboxService(tmp_path)
    args = [f"argument-{index}" for index in range(25)]

    result = await service.run_tool(
        {"tool_id": "args-tool", "request_id": "args-ok", "args": args}
    )
    rejected = await service.run_tool(
        {
            "tool_id": "args-tool",
            "request_id": "args-rejected",
            "args": ["x"] * (MAX_TOOL_ARGUMENTS + 1),
        }
    )

    assert result["ok"] is True
    assert json.loads(result["stdout"]) == args
    assert rejected["ok"] is False
    assert rejected["error_code"] == "TOO_MANY_ARGUMENTS"
    assert rejected["argument_count"] == MAX_TOOL_ARGUMENTS + 1
    assert rejected["max_arguments"] == MAX_TOOL_ARGUMENTS


def test_tool_environment_uses_allowlist_and_preserves_required_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "provider-key")
    monkeypatch.setenv(
        "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT",
        str(tmp_path / "assistant-state"),
    )
    monkeypatch.setenv("GPTBRIDGE_AI_ASSISTANT_PROFILE", "retirement")
    monkeypatch.setenv("GPTBRIDGE_LOCAL_LLM_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("GPTBRIDGE_LOCAL_LLM_PROFILE", "compact")
    monkeypatch.setenv("GPTBRIDGE_LOCAL_LLM_NUM_CTX", "2048")
    monkeypatch.setenv("GPTBRIDGE_LOCAL_LLM_NUM_PREDICT", "256")
    monkeypatch.setenv("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ENABLED", "1")
    monkeypatch.setenv("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ALLOW_LAN", "0")
    monkeypatch.setenv("FILE_SORTER_STATE_ROOT", str(tmp_path / "sorter-state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setenv("GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE", "1")
    monkeypatch.setenv("GPTBRIDGE_MANAGED_BACKEND_TOOL_ID", "ambient-tool")
    monkeypatch.setenv(
        "GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID",
        "ambient-workspace",
    )
    monkeypatch.setenv("GPTBRIDGE_MANAGED_BACKEND_VERSION", "ambient-version")
    monkeypatch.setenv(
        "GPTBRIDGE_CLEANER_TARGET_ROOT",
        str(tmp_path / "cleaner-target"),
    )
    monkeypatch.setenv("UNRELATED_SECRET_TOKEN", "must-not-leak")
    tool_dir = tmp_path / "platform_tools" / "env-tool"

    service = ToolboxService(tmp_path)
    child_env = service._tool_environment("env-tool", tool_dir)
    assistant_env = service._tool_environment(
        "assistant-tool",
        tool_dir,
        {
            "environment": {
                "allow": [
                    "ALPHAVANTAGE_API_KEY",
                    "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT",
                    "GPTBRIDGE_AI_ASSISTANT_PROFILE",
                    "GPTBRIDGE_LOCAL_LLM_URL",
                    "GPTBRIDGE_LOCAL_LLM_PROFILE",
                    "GPTBRIDGE_LOCAL_LLM_NUM_CTX",
                    "GPTBRIDGE_LOCAL_LLM_NUM_PREDICT",
                    "GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ENABLED",
                    "GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ALLOW_LAN",
                    "UNRELATED_SECRET_TOKEN",
                ]
            }
        },
    )
    sorter_env = service._tool_environment(
        "sorter-tool",
        tool_dir,
        {"environment": {"allow": ["FILE_SORTER_STATE_ROOT"]}},
    )
    cleaner_env = service._tool_environment(
        "project-cleaner",
        tool_dir,
        {
            "permissions": {"allow_modify": ["authorized-project"]},
            "capabilities": {
                "system-rescue": {"authority": "project-root-only"},
            },
            "environment": {
                "allow": ["GPTBRIDGE_CLEANER_TARGET_ROOT"],
                "bindings": {
                    "GPTBRIDGE_CLEANER_TARGET_ROOT": "project_root",
                },
            },
        },
    )
    trusted_start_env = service._tool_environment(
        "env-tool",
        tool_dir,
        {"version": "9.1.0"},
        trusted_managed_backend_reuse=True,
    )
    hidden_start_env = service._tool_environment(
        "env-tool",
        tool_dir,
        start_hidden=True,
    )

    assert child_env["PATH"] == "test-path"
    assert child_env["TEMP"] == str(tmp_path / "temp")
    assert child_env["XDG_STATE_HOME"] == str(tmp_path / "xdg-state")
    assert child_env["GPTBRIDGE_PROJECT_ROOT"] == str(tool_dir.resolve())
    assert child_env["GPTBRIDGE_TOOL_ID"] == "env-tool"
    assert child_env["GPTBRIDGE_TOOL_DIR"] == str(tool_dir)
    assert child_env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert child_env["PYTHONNOUSERSITE"] == "1"
    source = Path("src-core/tasks/toolbox_service.py").read_text(encoding="utf-8")
    assert 'str(python_executable),\n            "-B",\n            "-s",' in source
    assert "ELECTRON_RUN_AS_NODE" not in child_env
    assert "GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE" not in child_env
    assert "GPTBRIDGE_MANAGED_BACKEND_TOOL_ID" not in child_env
    assert "GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID" not in child_env
    assert "GPTBRIDGE_MANAGED_BACKEND_VERSION" not in child_env
    assert "UNRELATED_SECRET_TOKEN" not in child_env
    assert "ALPHAVANTAGE_API_KEY" not in child_env
    assert "GPTBRIDGE_LOCAL_LLM_URL" not in child_env
    assert "FILE_SORTER_STATE_ROOT" not in child_env
    assert "GPTBRIDGE_CLEANER_TARGET_ROOT" not in child_env
    assert assistant_env["ALPHAVANTAGE_API_KEY"] == "provider-key"
    assert assistant_env["GPTBRIDGE_AI_ASSISTANT_DATA_ROOT"] == str(
        tmp_path / "assistant-state"
    )
    assert assistant_env["GPTBRIDGE_AI_ASSISTANT_PROFILE"] == "retirement"
    assert assistant_env["GPTBRIDGE_LOCAL_LLM_URL"] == "http://127.0.0.1:11434"
    assert assistant_env["GPTBRIDGE_LOCAL_LLM_PROFILE"] == "compact"
    assert assistant_env["GPTBRIDGE_LOCAL_LLM_NUM_CTX"] == "2048"
    assert assistant_env["GPTBRIDGE_LOCAL_LLM_NUM_PREDICT"] == "256"
    assert assistant_env["GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ENABLED"] == "1"
    assert assistant_env["GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ALLOW_LAN"] == "0"
    assert "FILE_SORTER_STATE_ROOT" not in assistant_env
    assert sorter_env["FILE_SORTER_STATE_ROOT"] == str(tmp_path / "sorter-state")
    assert "ALPHAVANTAGE_API_KEY" not in sorter_env
    assert cleaner_env["GPTBRIDGE_CLEANER_TARGET_ROOT"] == str(tmp_path.resolve())
    assert trusted_start_env["GPTBRIDGE_TRUSTED_MANAGED_BACKEND_REUSE"] == "1"
    assert trusted_start_env["GPTBRIDGE_MANAGED_BACKEND_TOOL_ID"] == "env-tool"
    normalized_root = str(tmp_path.resolve()).replace("\\", "/")
    if os.name == "nt":
        normalized_root = normalized_root.lower()
    expected_workspace_id = hashlib.sha256(
        normalized_root.encode("utf-8")
    ).hexdigest()[:24]
    assert (
        trusted_start_env["GPTBRIDGE_MANAGED_BACKEND_WORKSPACE_INSTANCE_ID"]
        == expected_workspace_id
    )
    assert trusted_start_env["GPTBRIDGE_MANAGED_BACKEND_VERSION"] == "9.1.0"
    assert hidden_start_env["GPTBRIDGE_START_HIDDEN"] == "1"
    assert "GPTBRIDGE_START_HIDDEN" not in child_env


def test_standalone_tool_preserves_validated_host_project_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_root = tmp_path / "host"
    standalone_root = tmp_path / "standalone"
    tool_id = "project-cleaner"
    host_manifest = host_root / "platform_tools" / tool_id / "manifest.json"
    standalone_tool_dir = standalone_root / "platform_tools" / tool_id
    host_manifest.parent.mkdir(parents=True)
    standalone_tool_dir.mkdir(parents=True)
    (host_root / "package.json").write_text("{}", encoding="utf-8")
    manifest = {
        "id": tool_id,
        "permissions": {"allow_modify": ["authorized-project"]},
        "capabilities": {
            "system-rescue": {"authority": "project-root-only"},
        },
        "environment": {
            "allow": ["GPTBRIDGE_CLEANER_TARGET_ROOT"],
            "bindings": {
                "GPTBRIDGE_CLEANER_TARGET_ROOT": "project_root",
            },
        },
    }
    host_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("GPTBRIDGE_CLEANER_TARGET_ROOT", str(host_root))

    service = ToolboxService(
        standalone_root,
        allowed_tool_ids={tool_id},
    )
    child_env = service._tool_environment(
        tool_id,
        standalone_tool_dir,
        manifest,
    )

    assert child_env["GPTBRIDGE_PROJECT_ROOT"] == str(standalone_tool_dir.resolve())
    assert child_env["GPTBRIDGE_CLEANER_TARGET_ROOT"] == str(host_root.resolve())


def test_standalone_tool_rejects_unvalidated_host_project_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standalone_root = tmp_path / "standalone"
    invalid_host_root = tmp_path / "not-a-project"
    tool_id = "project-cleaner"
    standalone_tool_dir = standalone_root / "platform_tools" / tool_id
    standalone_tool_dir.mkdir(parents=True)
    invalid_host_root.mkdir()
    monkeypatch.setenv(
        "GPTBRIDGE_CLEANER_TARGET_ROOT",
        str(invalid_host_root),
    )
    manifest = {
        "permissions": {"allow_modify": ["authorized-project"]},
        "capabilities": {
            "system-rescue": {"authority": "project-root-only"},
        },
        "environment": {
            "allow": ["GPTBRIDGE_CLEANER_TARGET_ROOT"],
            "bindings": {
                "GPTBRIDGE_CLEANER_TARGET_ROOT": "project_root",
            },
        },
    }

    service = ToolboxService(
        standalone_root,
        allowed_tool_ids={tool_id},
    )
    child_env = service._tool_environment(
        tool_id,
        standalone_tool_dir,
        manifest,
    )

    assert child_env["GPTBRIDGE_CLEANER_TARGET_ROOT"] == str(
        standalone_root.resolve()
    )


@pytest.mark.asyncio
async def test_run_rejects_working_directory_escape(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "cwd-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (entry_dir / "main.py").write_text("print('must not run')\n", encoding="utf-8")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "cwd-tool",
                "name": "CWD Tool",
                "enabled": True,
                "runtime": {
                    "entry": "src/main.py",
                    "workingDirectory": "../..",
                },
            }
        ),
        encoding="utf-8",
    )

    result = await ToolboxService(tmp_path).run_tool(
        {
            "tool_id": "cwd-tool",
            "request_id": "cwd-escape-request",
            "args": [],
        }
    )

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_RUNTIME_PATH"


@pytest.mark.asyncio
async def test_run_rejects_linked_entry_outside_tool(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "link-entry-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("print('must not run')\n", encoding="utf-8")
    linked_entry = entry_dir / "main.py"
    try:
        linked_entry.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "link-entry-tool",
                "name": "Link Entry Tool",
                "enabled": True,
                "runtime": {
                    "entry": "src/main.py",
                    "workingDirectory": ".",
                },
            }
        ),
        encoding="utf-8",
    )

    result = await ToolboxService(tmp_path).run_tool(
        {
            "tool_id": "link-entry-tool",
            "request_id": "link-entry-request",
            "args": [],
        }
    )

    assert result["ok"] is False
    assert result["error_code"] == "INVALID_ENTRY_PATH"


@pytest.mark.asyncio
async def test_timeout_result_keeps_partial_output_and_structured_error(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "platform_tools" / "timeout-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "timeout-tool",
                "name": "Timeout Tool",
                "enabled": True,
                "entry": "platform_tools/timeout-tool/src/main",
                "timeout_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    (entry_dir / "main.py").write_text(
        "\n".join(
            [
                "import sys, time",
                "print('stdout-before-timeout', flush=True)",
                "print('stderr-before-timeout', file=sys.stderr, flush=True)",
                "time.sleep(30)",
            ]
        ),
        encoding="utf-8",
    )

    result = await ToolboxService(tmp_path).run_tool(
        {
            "tool_id": "timeout-tool",
            "request_id": "timeout-request",
            "args": [],
        }
    )

    assert result["ok"] is False
    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert result["cancelled"] is False
    assert result["request_id"] == "timeout-request"
    assert "stdout-before-timeout" in result["stdout"]
    assert "stderr-before-timeout" in result["stderr"]
    assert result["output"]["stdout"] == result["stdout"]
    assert result["output"]["stderr"] == result["stderr"]
    assert result["error"] == {
        "code": "TOOL_TIMEOUT",
        "message": "Tool timed out after 1 seconds",
        "timeout_seconds": 1,
    }


@pytest.mark.asyncio
async def test_tool_output_is_drained_but_bounded(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "output-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "output-tool",
                "name": "Output Tool",
                "enabled": True,
                "entry": "platform_tools/output-tool/src/main",
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (entry_dir / "main.py").write_text(
        "\n".join(
            [
                "import sys",
                f"print('x' * {MAX_TOOL_OUTPUT_CHARS + 4096})",
                f"print('y' * {MAX_TOOL_OUTPUT_CHARS + 4096}, file=sys.stderr)",
            ]
        ),
        encoding="utf-8",
    )

    result = await ToolboxService(tmp_path).run_tool(
        {
            "tool_id": "output-tool",
            "request_id": "bounded-output-request",
            "args": [],
        }
    )

    assert result["ok"] is True
    assert len(result["stdout"]) == MAX_TOOL_OUTPUT_CHARS
    assert len(result["stderr"]) == MAX_TOOL_OUTPUT_CHARS
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert result["output"]["stdout_truncated"] is True
    assert result["output"]["stderr_truncated"] is True


@pytest.mark.asyncio
async def test_request_scoped_tracking_rejects_same_tool_overlap_and_exact_cancel(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "platform_tools" / "exclusive-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "exclusive-tool",
                "name": "Exclusive Tool",
                "enabled": True,
                "entry": "platform_tools/exclusive-tool/src/main",
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    entry_file = entry_dir / "main.py"
    entry_file.write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    service = ToolboxService(tmp_path)
    first_task = asyncio.create_task(
        service.run_tool(
            {
                "tool_id": "exclusive-tool",
                "request_id": "request-one",
                "args": [],
            }
        )
    )
    for _ in range(80):
        if "request-one" in service._running_processes:
            break
        await asyncio.sleep(0.025)

    assert set(service._running_processes) == {"request-one"}
    overlapping = await service.run_tool(
        {
            "tool_id": "exclusive-tool",
            "request_id": "request-two",
            "args": [],
        }
    )
    wrong_cancel = await service.cancel_tool_run(
        {
            "tool_id": "exclusive-tool",
            "request_id": "request-two",
        }
    )
    assert overlapping["ok"] is False
    assert overlapping["error_code"] == "TOOL_BUSY"
    assert overlapping["active_request_id"] == "request-one"
    assert wrong_cancel["ok"] is False
    assert wrong_cancel["error_code"] == "RUN_NOT_FOUND"
    assert first_task.done() is False
    assert entry_file.read_text(encoding="utf-8").startswith("import time")

    cancel_result = await service.cancel_tool_run(
        {
            "tool_id": "exclusive-tool",
            "request_id": "request-one",
        }
    )
    first_result = await asyncio.wait_for(first_task, timeout=5)

    assert cancel_result["ok"] is True
    assert cancel_result["request_id"] == "request-one"
    assert first_result["status"] == "cancelled"
    assert first_result["request_id"] == "request-one"
    assert service._running_processes == {}


@pytest.mark.asyncio
async def test_cancel_tool_run_requires_exact_request_id(tmp_path: Path) -> None:
    service = ToolboxService(tmp_path)

    result = await service.cancel_tool_run({"tool_id": "some-tool"})

    assert result["ok"] is False
    assert result["error_code"] == "MISSING_REQUEST_ID"
