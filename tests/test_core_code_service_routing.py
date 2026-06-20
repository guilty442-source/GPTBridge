from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from ipc.handlers import CommandRouter


class FakeModeManager:
    active_mode = "full"

    def can_execute_command(self, _command: str) -> bool:
        return True


class BlockingModeManager:
    active_mode = "safe"

    def can_execute_command(self, _command: str) -> bool:
        return False


class FakeApp:
    def __init__(self) -> None:
        self.mode_manager = FakeModeManager()


class FakeCoreCodeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def diagnose_code(self, target_path: str = "", content: str | None = None) -> dict:
        self.calls.append(("diagnose_code", target_path))
        return {"ok": True, "target_path": target_path, "content": content}

    async def run_unit_tests(self, target_path: str = "") -> dict:
        self.calls.append(("run_unit_tests", target_path))
        return {"ok": True, "target_path": target_path}


@pytest.mark.asyncio
async def test_app_code_command_routes_to_core_code_service() -> None:
    app = FakeApp()
    core_code_service = FakeCoreCodeService()
    router = CommandRouter(
        app=app,
        core_code_service=core_code_service,
        mode_services={},
    )

    event, payload = await router.handle(
        "app:run-unit-tests",
        {"path": "src-core/main.py"},
    )

    assert event == "app:run-unit-tests_result"
    assert payload == {"ok": True, "target_path": "src-core/main.py"}
    assert core_code_service.calls == [("run_unit_tests", "src-core/main.py")]


@pytest.mark.asyncio
async def test_diagnose_code_command_routes_to_core_code_service() -> None:
    app = FakeApp()
    core_code_service = FakeCoreCodeService()
    router = CommandRouter(
        app=app,
        core_code_service=core_code_service,
        mode_services={},
    )

    event, payload = await router.handle(
        "app:diagnose-code",
        {"path": "src-core/main.py", "content": "print('x')\n"},
    )

    assert event == "app:diagnose-code_result"
    assert payload == {
        "ok": True,
        "target_path": "src-core/main.py",
        "content": "print('x')\n",
    }
    assert core_code_service.calls == [("diagnose_code", "src-core/main.py")]


@pytest.mark.asyncio
async def test_app_code_command_requires_core_code_service() -> None:
    app = FakeApp()
    router = CommandRouter(app=app, mode_services={})

    event, payload = await router.handle("app:run-unit-tests", {})

    assert event == "app:run-unit-tests_result"
    assert payload["ok"] is False
    assert "Core code service" in payload["message"]


@pytest.mark.asyncio
async def test_blocked_command_uses_original_result_event() -> None:
    app = FakeApp()
    app.mode_manager = BlockingModeManager()
    router = CommandRouter(app=app, mode_services={})

    event, payload = await router.handle("vaultly_get_state", {})

    assert event == "vaultly_get_state_result"
    assert payload["ok"] is False
    assert payload["command"] == "vaultly_get_state"
