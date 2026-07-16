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
    def __init__(
        self,
        toolbox_service: object,
        command_result: dict | None = None,
    ) -> None:
        self.toolbox_service = toolbox_service
        self.command_result = command_result
        self.scope = "standalone"

    async def handle(self, command: str, _payload: dict) -> tuple[str, dict]:
        if self.command_result is None:
            raise AssertionError(f"unexpected command: {command}")
        return f"{command}_result", self.command_result


class DummyApp:
    def __init__(
        self,
        toolbox_service: object,
        core_logger: object | None = None,
        command_result: dict | None = None,
    ) -> None:
        self.command_router = DummyRouter(toolbox_service, command_result)
        self.task_queue = None
        self.core_logger = core_logger


class SuccessfulToolboxService:
    async def run_tool(self, payload: dict, event_callback: object | None = None) -> dict:
        _ = event_callback
        return {"ok": True, "tool_id": payload["tool_id"], "stdout": ""}


class FailingToolboxService:
    async def run_tool(self, payload: dict, event_callback: object | None = None) -> dict:
        _ = payload, event_callback
        raise RuntimeError("tool crashed")


class FailingLogger:
    def write(self, *_args: object, **_kwargs: object) -> None:
        raise OSError("log destination unavailable")


class CapturingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, object]] = []

    def write(self, category: str, message: str, payload: object) -> None:
        self.records.append((category, message, payload))


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
    assert payload["message"] == "Command failed; consult the local error log."
    assert payload["error_id"]


@pytest.mark.asyncio
async def test_completed_tool_result_survives_core_logger_failure() -> None:
    ui = DummyUi()

    await process_command_task(
        DummyApp(SuccessfulToolboxService(), core_logger=FailingLogger()),
        ui,
        "toolbox_run_tool",
        {"tool_id": "file-sorter", "request_id": "req-log", "args": []},
    )

    assert ui.events == [
        (
            "toolbox_run_tool_result",
            {
                "ok": True,
                "tool_id": "file-sorter",
                "stdout": "",
                "request_id": "req-log",
            },
        )
    ]
    assert ui.errors == []


@pytest.mark.asyncio
async def test_investment_watch_result_logs_summary_without_large_sections() -> None:
    ui = DummyUi()
    logger = CapturingLogger()
    result = {
        "ok": True,
        "message": "portfolio imported",
        "state_revision": "revision-2",
        "state": {
            "updated_at": "2026-07-29T12:00:00+08:00",
            "portfolio": {
                "file_name": "holdings.xlsx",
                "holding_count": 2,
            },
            "holdings": [{"symbol": "AAPL"}, {"symbol": "2330"}],
            "portfolio_versions": [{"version_id": "v1"}],
            "analytics": {
                "transactions": [{"transaction_id": "tx-1"}],
                "events": [{"event_id": "event-1"}, {"event_id": "event-2"}],
            },
        },
        "diagnostics": {
            "state": "ready",
            "state_label": "Ready",
            "warnings": [{"message": "review quote"}],
        },
        "excel_mapping_preview": {
            "sheet_count": 2,
            "selected_sheet_name": "Holdings",
            "mapping_fields": ["symbol", "quantity"],
            "required_fields": ["symbol"],
            "sheets": [
                {"sheet_name": "Holdings", "rows": [["AAPL"], ["2330"]]},
                {"sheet_name": "Summary", "rows": [["Total"]]},
            ],
        },
        "summary": {"holding_count": 2, "imported_count": 2},
    }

    await process_command_task(
        DummyApp(object(), core_logger=logger, command_result=result),
        ui,
        "investment_watch_import_excel_mapping",
        {"request_id": "req-investment"},
    )

    event, event_payload = ui.events[0]
    assert event == "investment_watch_import_excel_mapping_result"
    assert event_payload["state"] == result["state"]
    assert event_payload["diagnostics"] == result["diagnostics"]
    assert event_payload["excel_mapping_preview"] == result["excel_mapping_preview"]
    assert event_payload["request_id"] == "req-investment"

    assert len(logger.records) == 1
    category, message, log_payload = logger.records[0]
    assert category == "core"
    assert message == "investment_watch_import_excel_mapping result"
    assert isinstance(log_payload, dict)
    assert "state" not in log_payload
    assert "diagnostics" not in log_payload
    assert "excel_mapping_preview" not in log_payload
    assert log_payload["request_id"] == "req-investment"
    assert log_payload == {
        "command": "investment_watch_import_excel_mapping",
        "ok": True,
        "tool_id": "",
        "request_id": "req-investment",
        "error_code": "",
    }


@pytest.mark.asyncio
async def test_investment_watch_unchanged_state_result_skips_core_log() -> None:
    ui = DummyUi()
    logger = CapturingLogger()
    result = {
        "ok": True,
        "not_modified": True,
        "state_revision": "revision-1",
    }

    await process_command_task(
        DummyApp(object(), core_logger=logger, command_result=result),
        ui,
        "investment_watch_get_state",
        {"request_id": "req-state"},
    )

    assert logger.records == [
        (
            "core",
            "investment_watch_get_state result",
            {
                "command": "investment_watch_get_state",
                "ok": True,
                "tool_id": "",
                "request_id": "req-state",
                "error_code": "",
            },
        )
    ]
    assert ui.events == [
        (
            "investment_watch_get_state_result",
            {
                "ok": True,
                "not_modified": True,
                "state_revision": "revision-1",
                "request_id": "req-state",
            },
        )
    ]
