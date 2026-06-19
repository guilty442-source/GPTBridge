from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from ipc.server import process_command_task


class DummyUi:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.errors: list[str] = []
        self.logs: list[str] = []

    async def send_event(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    async def send_error(self, message: str) -> None:
        self.errors.append(message)

    async def send_log(self, message: str) -> None:
        self.logs.append(message)


class DummyRouter:
    def __init__(self, toolbox_service: object) -> None:
        self.toolbox_service = toolbox_service


class DummyApp:
    def __init__(self, toolbox_service: object) -> None:
        self.command_router = DummyRouter(toolbox_service)
        self.task_queue = None
        self.core_logger = None


class SuccessfulToolboxService:
    async def run_tool(self, payload: dict, event_callback: object | None = None) -> dict:
        _ = event_callback
        return {"ok": True, "tool_id": payload["tool_id"], "stdout": ""}


class FailingToolboxService:
    async def run_tool(self, payload: dict, event_callback: object | None = None) -> dict:
        _ = payload, event_callback
        raise RuntimeError("tool crashed")


@pytest.mark.asyncio
async def test_toolbox_run_result_preserves_request_id() -> None:
    ui = DummyUi()

    await process_command_task(
        DummyApp(SuccessfulToolboxService()),
        ui,
        "toolbox_run_tool",
        {"tool_id": "file-sorter", "request_id": "req-123", "args": []},
    )

    assert ui.events == [
        (
            "toolbox_run_tool_result",
            {
                "ok": True,
                "tool_id": "file-sorter",
                "stdout": "",
                "request_id": "req-123",
            },
        )
    ]


@pytest.mark.asyncio
async def test_toolbox_run_error_preserves_request_id() -> None:
    ui = DummyUi()

    await process_command_task(
        DummyApp(FailingToolboxService()),
        ui,
        "toolbox_run_tool",
        {"tool_id": "file-sorter", "request_id": "req-456", "args": []},
    )

    event, payload = ui.events[0]
    assert event == "toolbox_run_tool_result"
    assert payload["ok"] is False
    assert payload["tool_id"] == "file-sorter"
    assert payload["request_id"] == "req-456"
    assert payload["message"] == "tool crashed"
