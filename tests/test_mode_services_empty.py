import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from ipc.handlers import CommandRouter
from modes.mode_manager import ModeManager


@pytest.mark.asyncio
async def test_list_mode_services_empty():
    class App:
        def __init__(self):
            self.project_root = None
            self.command_router = None
            self._log = lambda *_: None

    app = App()
    mm = ModeManager(app)
    # do not register any services
    app.mode_manager = mm

    app.command_router = CommandRouter(
        app=app,
        session=None,
        chatgpt=None,
        gemini=None,
        backup_manager=None,
        history_manager=None,
        orchestrator=None,
        autonomous_agent=None,
        toolbox_service=None,
        developer_service=None,
        rescue_service=None,
        settings_service=None,
        mode_services=mm._mode_services,
    )

    event, payload = await app.command_router.handle("app:list-mode-services", {})

    assert isinstance(event, str)
    assert isinstance(payload, dict)
    assert payload.get("ok") is True
    assert payload.get("services") == []


def test_safe_mode_allows_agent_coder_ai_commands():
    class App:
        def __init__(self):
            self.project_root = None
            self.command_router = None
            self._log = lambda *_: None
            self.core_logger = None

    app = App()
    mm = ModeManager(app)
    mm.set_active_mode("safe")

    assert mm.can_execute_command("discussion_query")
    assert mm.can_execute_command("app:agent-instruct")
    assert mm.can_execute_command("app:agent-intervention")
    assert mm.can_execute_command("app:run-unit-tests")


@pytest.mark.asyncio
async def test_safe_mode_registers_and_allows_child_tool_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class ChildService:
        def __init__(self):
            self.started = False

        def owns(self, command: str) -> bool:
            return command == "vaultly_get_state"

        async def start(self) -> None:
            self.started = True

        async def handle(self, command: str, payload: dict, latest_ai_answer=None):
            return f"{command}_result", {"ok": True, "platforms": []}

    class Definition:
        service_name = "vaultly"
        tool_dir_name = "vaultly"
        package_name = "vaultly"
        class_name = "VaultlyService"

    child_service = ChildService()

    class FakeRegistry:
        def __init__(self, _project_root):
            pass

        def discover(self):
            return [Definition()]

        def create_service(self, _definition, _project_root):
            return child_service

    class App:
        def __init__(self):
            self.project_root = tmp_path
            self.command_router = None
            self.core_code_service = None
            self.toolbox_service = None
            self.rescue_service = None
            self.history_manager = None
            self.core_logger = None
            self._log = lambda *_: None

    monkeypatch.setattr("modes.mode_manager.ChildToolServiceRegistry", FakeRegistry)
    app = App()
    mm = ModeManager(app)
    app.mode_manager = mm

    await mm.initialize_safe_mode()
    event, payload = await app.command_router.handle("vaultly_get_state", {})

    assert child_service.started is True
    assert mm.can_execute_command("vaultly_get_state") is True
    assert "vaultly" in mm._mode_services
    assert event == "vaultly_get_state_result"
    assert payload["ok"] is True
