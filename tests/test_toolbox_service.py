from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from tasks.toolbox_service import ToolboxService, _background_subprocess_kwargs


def test_project_size_bytes_matches_actual_tool_directory_size(tmp_path: Path) -> None:
    tool_dir = tmp_path / "platform_tools" / "size-tool"
    (tool_dir / "src").mkdir(parents=True)
    (tool_dir / "dist").mkdir()
    (tool_dir / "build").mkdir()
    (tool_dir / "node_modules" / "pkg").mkdir(parents=True)
    (tool_dir / "src" / "main.py").write_bytes(b"a" * 10)
    (tool_dir / "dist" / "size-tool.exe").write_bytes(b"b" * 20)
    (tool_dir / "build" / "artifact.bin").write_bytes(b"c" * 30)
    (tool_dir / "node_modules" / "pkg" / "index.js").write_bytes(b"d" * 40)

    actual_size = sum(path.stat().st_size for path in tool_dir.rglob("*") if path.is_file())

    assert ToolboxService._project_size_bytes(tool_dir) == actual_size


def test_background_subprocess_kwargs_hide_windows_console() -> None:
    kwargs = _background_subprocess_kwargs()

    if os.name == "nt" and getattr(subprocess, "CREATE_NO_WINDOW", 0):
        assert kwargs == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert kwargs == {}


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

    async def fail_create_subprocess_exec(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("start_tool should not spawn an already-running EXE")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_create_subprocess_exec)

    result = await ToolboxService(tmp_path).start_tool({"tool_id": "external-tool"})

    assert result["ok"] is True
    assert result["pid"] == 12345
    assert result["message"] == "Tool executable is already running"


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
        ("open_tool_code", {"tool_id": stale_tool_id, "no_external": True}),
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
            {"tool_id": "slow-tool", "args": []},
            event_callback=event_callback,
        )
    )
    for _ in range(40):
        if "slow-tool" in service._running_processes:
            break
        await asyncio.sleep(0.05)

    cancel_result = await service.cancel_tool_run({"tool_id": "slow-tool"})
    run_result = await asyncio.wait_for(run_task, timeout=5)

    assert cancel_result["ok"] is True
    assert run_result["ok"] is False
    assert run_result["cancelled"] is True
    assert run_result["message"] == "Tool cancelled by user"
    assert "started" in run_result["stdout"]
    assert "slow-tool" not in service._running_processes


@pytest.mark.asyncio
async def test_streaming_tool_run_forwards_investment_progress(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "platform_tools" / "investment-tool"
    entry_dir = tool_dir / "src"
    entry_dir.mkdir(parents=True)
    (tool_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "investment-tool",
                "name": "Investment Tool",
                "version": "1.0.0",
                "status": "stopped",
                "enabled": True,
                "entry": "platform_tools/investment-tool/src/main",
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
                "print('INVESTMENT_MANAGER_PROGRESS_JSON=' + json.dumps(payload), flush=True)",
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
        {"tool_id": "investment-tool", "args": []},
        event_callback=event_callback,
    )

    assert result["ok"] is True
    assert "finished" in result["stdout"]
    assert events == [
        (
            "toolbox_run_tool_progress",
            {
                "ok": True,
                "tool_id": "investment-tool",
                "phase": "quote_snapshot",
                "message": "done",
            },
        )
    ]
