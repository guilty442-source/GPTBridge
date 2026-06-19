import os
import sys
import pytest

# ensure imports from src-core
sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from modes.mode_manager import ModeManager


class FakeService2:
    def __init__(self):
        pass
    def owns(self, command: str) -> bool:
        return command == "other_cmd"
    async def handle(self, command: str, payload: dict, latest_ai_answer: str | None = None):
        return ("other_cmd_result", {"ok": True})


@pytest.mark.asyncio
async def test_list_mode_services_and_unknown_command():
    class App:
        def __init__(self):
            self.project_root = None
            self.command_router = None
            self._log = lambda *_: None
            self.core_logger = None

    app = App()
    mm = ModeManager(app)
    svc = FakeService2()
    mm.register_mode_service("other", svc)
    app.mode_manager = mm

    from ipc.handlers import CommandRouter

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

    # list services
    event, payload = await app.command_router.handle("app:list-mode-services", {})
    assert isinstance(event, str)
    assert isinstance(payload, dict)
    assert payload.get("ok") is True
    assert "other" in payload.get("services", [])

    # unknown command: should return an event and payload with at least 'ok'
    event2, payload2 = await app.command_router.handle("nonexistent_cmd", {})
    assert isinstance(event2, str)
    assert isinstance(payload2, dict)
    assert "ok" in payload2
