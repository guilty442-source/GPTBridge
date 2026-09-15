import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from core_system.module_automation_registry import module_automation_status


class _Bad:
    def get_status(self):
        raise RuntimeError("boom")


class _Good:
    def __init__(self, state: str) -> None:
        self._state = state

    def get_status(self):
        return {"state": self._state, "last_error": ""}


class _App:
    hot_reload_watcher = None
    update_manager = _Good("running")
    authority_reanchor_service = _Bad()
    daily_global_cleaner_service = _Good("running")
    maintenance_sovereign = _Good("started")


def test_failing_unit_is_isolated() -> None:
    report = module_automation_status(_App())
    by_unit = {item["unit"]: item for item in report}

    assert by_unit["authority-reanchor"]["state"] == "error"
    assert "boom" in by_unit["authority-reanchor"]["last_error"]
    assert by_unit["authority-reanchor"]["isolated"] is True
    assert by_unit["update-manager"]["state"] == "running"
    assert by_unit["daily-cleaner"]["state"] == "running"
    assert by_unit["hot-reload"]["state"] == "unavailable"
    assert len(report) == 5
