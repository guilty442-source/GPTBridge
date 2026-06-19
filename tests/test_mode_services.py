import os
import sys
import pytest

# Ensure src-core is importable during tests by adding it to sys.path
sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from modes.mode_manager import ModeManager


class FakeService:
    VERSION = "1.2.3"
    def __init__(self):
        self.workspace = None
    def owns(self, command: str) -> bool:
        return command == "fake_cmd"
    async def handle(self, command: str, payload: dict, latest_ai_answer: str | None = None):
        return ("fake_cmd_result", {"ok": True, "cmd": command, "latest": latest_ai_answer})


@pytest.mark.asyncio
async def test_register_and_ipc_status():
    class App:
        def __init__(self):
            self.project_root = None
            self.command_router = None
            self._log = lambda *_: None
            self.core_logger = None

    app = App()
    mm = ModeManager(app)
    svc = FakeService()
    mm.register_mode_service("fake", svc)
    # create a minimal CommandRouter instance using the registered mode services
    from ipc.handlers import CommandRouter

    # attach ModeManager to satisfy CommandRouter expectations
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

    # simulate IPC call directly via command_router
    event, payload = await app.command_router.handle("app:get-mode-services-status", {})
    assert event == "app:get-mode-services-status_result"
    assert payload["ok"] is True
    assert "fake" in payload["services"]

    # test dispatch to service
    event2, payload2 = await app.command_router.handle("fake_cmd", {"latest_ai_answer": "answer"})
    assert event2 == "fake_cmd_result"
    assert payload2["ok"] is True
    assert payload2["latest"] == "answer"
