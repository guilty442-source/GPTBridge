import os
import sys
import pytest

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
