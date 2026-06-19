from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from tasks.toolbox_service import ToolboxService


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
