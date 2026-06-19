import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from ipc.handlers import CommandRouter
from modes.mode_manager import ModeManager


class AnswerService:
    def owns(self, command: str) -> bool:
        return command == "answer_cmd"

    async def handle(self, command: str, payload: dict, latest_ai_answer: str | None = None):
        # echo back what was received
        return ("answer_cmd_result", {"ok": True, "latest": latest_ai_answer})


@pytest.mark.asyncio
async def test_latest_ai_answer_none_forwarded():
    class App:
        def __init__(self):
            self.project_root = None
            self.command_router = None
            self._log = lambda *_: None

    app = App()
    mm = ModeManager(app)
    svc = AnswerService()
    mm.register_mode_service("ans", svc)
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

    event, payload = await app.command_router.handle("answer_cmd", {})
    assert event == "answer_cmd_result"
    assert payload["ok"] is True
    assert payload["latest"] is None
