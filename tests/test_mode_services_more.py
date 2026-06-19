import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from modes.mode_manager import ModeManager


class DummyService:
    def owns(self, command: str) -> bool:
        return False
    async def handle(self, command: str, payload: dict, latest_ai_answer: str | None = None):
        return ("noop", {"ok": True})


def test_register_duplicate_service():
    class App:
        def __init__(self):
            self.project_root = None
            self.command_router = None
            self._log = lambda *_: None

    app = App()
    mm = ModeManager(app)
    svc = DummyService()
    mm.register_mode_service("dup", svc)
    # register again with same key should replace or keep without error
    mm.register_mode_service("dup", svc)
    assert "dup" in mm._mode_services
