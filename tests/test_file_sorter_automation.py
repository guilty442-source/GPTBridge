from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

import main as main_module
from tasks.platform_automation import PlatformAutomationManager

automation_module_path = Path(
    "platform_tools/file-sorter/src/backend/automation_service.py"
)
automation_spec = importlib.util.spec_from_file_location(
    "test_file_sorter_automation_service",
    automation_module_path,
)
assert automation_spec and automation_spec.loader
automation_module = importlib.util.module_from_spec(automation_spec)
automation_spec.loader.exec_module(automation_module)
FileSorterAutomationService = automation_module.FileSorterAutomationService


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def write(
        self,
        category: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        self.records.append((category, message, payload))


class FakeRunner:
    def __init__(
        self,
        reports: list[dict[str, Any]] | None = None,
        *,
        targets: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.reports = reports or []
        self.targets = targets or []

    def recover_transactions(self) -> list[dict[str, Any]]:
        self.calls.append("recover")
        return []

    def run_enabled_profiles_once(self) -> list[dict[str, Any]]:
        self.calls.append("run")
        return list(self.reports)

    def enabled_profile_targets(self) -> list[str]:
        return list(self.targets)


async def _wait_until(predicate: Any, timeout: float = 1.0) -> None:
    async def wait_loop() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_loop(), timeout=timeout)


@pytest.mark.asyncio
async def test_service_starts_one_persistent_loop_and_stops_safely(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    service = FileSorterAutomationService(
        tmp_path,
        runner=runner,
        poll_interval=0.01,
    )

    assert await service.start() is True
    assert service.running is True
    await _wait_until(lambda: runner.calls.count("run") >= 2)

    await service.stop()
    calls_after_stop = list(runner.calls)
    await asyncio.sleep(0.03)

    assert service.running is False
    assert runner.calls == calls_after_stop
    assert runner.calls[:4] == ["recover", "run", "recover", "run"]


@pytest.mark.asyncio
async def test_disabled_service_is_noop_and_empty_profiles_are_valid(
    tmp_path: Path,
) -> None:
    disabled_runner = FakeRunner()
    disabled = FileSorterAutomationService(
        tmp_path,
        runner=disabled_runner,
        enabled=False,
    )

    assert await disabled.start() is False
    assert await disabled.run_once() is False
    await disabled.stop()
    assert disabled_runner.calls == []

    empty_runner = FakeRunner()
    enabled = FileSorterAutomationService(
        tmp_path,
        runner=empty_runner,
        enabled=True,
    )
    assert await enabled.run_once() is True
    assert empty_runner.calls == ["recover", "run"]


@pytest.mark.asyncio
async def test_loop_recovers_from_exception_and_start_does_not_duplicate(
    tmp_path: Path,
) -> None:
    class FlakyRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.recovery_attempts = 0

        def recover_transactions(self) -> list[dict[str, Any]]:
            self.calls.append("recover")
            self.recovery_attempts += 1
            if self.recovery_attempts == 1:
                raise RuntimeError("token=do-not-write-this-value")
            return []

    runner = FlakyRunner()
    logger = RecordingLogger()
    service = FileSorterAutomationService(
        tmp_path,
        logger,
        runner=runner,
        poll_interval=0.01,
    )

    assert await service.start() is True
    assert await service.start() is False
    await _wait_until(lambda: runner.calls.count("run") >= 1)
    await service.stop()

    assert runner.recovery_attempts >= 2
    assert runner.calls[:3] == ["recover", "recover", "run"]
    serialized_logs = repr(logger.records)
    assert "do-not-write-this-value" not in serialized_logs
    assert "RuntimeError" in serialized_logs


@pytest.mark.asyncio
async def test_run_once_rejects_overlapping_pass(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingRunner(FakeRunner):
        def recover_transactions(self) -> list[dict[str, Any]]:
            self.calls.append("recover")
            entered.set()
            release.wait(timeout=1.0)
            return []

    runner = BlockingRunner()
    service = FileSorterAutomationService(tmp_path, runner=runner)

    first_pass = asyncio.create_task(service.run_once())
    assert await asyncio.to_thread(entered.wait, 1.0) is True
    assert await service.run_once() is False
    release.set()

    assert await first_pass is True
    assert runner.calls == ["recover", "run"]


@pytest.mark.asyncio
async def test_new_file_wakes_realtime_sort_and_schedules_stability_retry(
    tmp_path: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()

    class RealtimeRunner(FakeRunner):
        def run_enabled_profiles_once(self) -> list[dict[str, Any]]:
            self.calls.append("run")
            run_count = self.calls.count("run")
            return [
                {
                    "ok": True,
                    "moved_count": 1 if run_count >= 3 else 0,
                    "waiting_for_second_observation_count": (
                        1 if run_count == 2 else 0
                    ),
                }
            ]

    runner = RealtimeRunner(targets=[str(target)])
    service = FileSorterAutomationService(
        tmp_path,
        runner=runner,
        poll_interval=60,
        realtime_scan_interval=0.01,
        settle_retry_seconds=0.02,
    )

    assert await service.start() is True
    await _wait_until(lambda: runner.calls.count("run") >= 1)
    await asyncio.sleep(0.03)
    (target / "new-report.txt").write_text("complete", encoding="utf-8")

    await _wait_until(lambda: runner.calls.count("run") >= 3)
    await service.stop()

    assert runner.calls.count("run") == 3


@pytest.mark.asyncio
async def test_new_file_created_during_pass_is_not_missed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    entered = threading.Event()
    release = threading.Event()

    class GapRunner(FakeRunner):
        def run_enabled_profiles_once(self) -> list[dict[str, Any]]:
            self.calls.append("run")
            if self.calls.count("run") == 1:
                entered.set()
                release.wait(timeout=1)
            return []

    runner = GapRunner(targets=[str(target)])
    service = FileSorterAutomationService(
        tmp_path,
        runner=runner,
        poll_interval=60,
        realtime_scan_interval=0.01,
    )

    assert await service.start() is True
    assert await asyncio.to_thread(entered.wait, 1) is True
    (target / "arrived-during-pass.txt").write_text("ready", encoding="utf-8")
    release.set()

    await _wait_until(lambda: runner.calls.count("run") >= 2)
    await service.stop()

    assert runner.calls.count("run") == 2


@pytest.mark.asyncio
@pytest.mark.skip(reason="Legacy main-mode initialization was removed in v1.0")
async def test_mother_app_initialize_does_not_start_platform_automation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAutomation:
        instances: list["FakeAutomation"] = []

        def __init__(self, project_root: Path, logger: Any) -> None:
            self.project_root = project_root
            self.logger = logger
            self.starts = 0
            self.stops = 0
            self.instances.append(self)

        async def start(self) -> bool:
            self.starts += 1
            return True

        async def stop(self) -> None:
            self.stops += 1

    class FakeModeManager:
        async def initialize_safe_mode(self) -> None:
            return None

        def all_mode_services(self) -> list[Any]:
            return []

    class FakeProjectAgent:
        max_backup_count = 0

    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(main_module, "PlatformAutomationManager", FakeAutomation)
    monkeypatch.setattr(
        main_module,
        "load_config",
        lambda: {"auto_start": True, "auto_cycle": 60, "max_backup_count": 3},
    )
    monkeypatch.setattr(main_module, "ensure_backup_layout", lambda _root: None)

    app = main_module.GPTBridgeApp()
    app._startup_persistence_synced = True
    app.history_manager = object()  # type: ignore[assignment]
    app.project_agent = FakeProjectAgent()  # type: ignore[assignment]
    app.core_logger = RecordingLogger()  # type: ignore[assignment]
    app.enforcer = object()  # type: ignore[assignment]
    app.task_queue = object()  # type: ignore[assignment]
    app.core_code_service = object()  # type: ignore[assignment]
    app.toolbox_service = object()  # type: ignore[assignment]
    app.developer_service = object()  # type: ignore[assignment]
    app.rescue_service = object()  # type: ignore[assignment]
    app.mode_manager = FakeModeManager()  # type: ignore[assignment]

    await app.initialize(mode="safe")
    assert FakeAutomation.instances == []

    await app.shutdown()
    assert app.platform_automation is None


@pytest.mark.asyncio
async def test_main_starts_only_manifest_opted_in_tool_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opted_in = tmp_path / "platform_tools" / "file-sorter"
    opted_out = tmp_path / "platform_tools" / "other-tool"
    opted_in.mkdir(parents=True)
    opted_out.mkdir(parents=True)
    (opted_in / "manifest.json").write_text(
        json.dumps(
            {
                "id": "file-sorter",
                "startup": {"auto_start": True, "background": True},
            }
        ),
        encoding="utf-8",
    )
    (opted_out / "manifest.json").write_text(
        json.dumps({"id": "other-tool"}),
        encoding="utf-8",
    )

    class FakeToolbox:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        async def start_tool(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.requests.append(payload)
            return {"ok": True}

    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    app = main_module.GPTBridgeApp()
    toolbox = FakeToolbox()
    app.toolbox_service = toolbox  # type: ignore[assignment]
    app._log = lambda _event: None  # type: ignore[method-assign]

    await app._start_manifest_background_tools()

    assert toolbox.requests == [
        {
            "tool_id": "file-sorter",
            "request_id": "startup:file-sorter",
            "background": True,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.skip(reason="Standalone loading now uses RuntimeBootstrap without modes")
async def test_standalone_initialization_loads_only_child_tool_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeModeManager:
        safe_initializations = 0

        async def initialize_safe_mode(
            self,
            *,
            standalone_tool_id: str | None = None,
        ) -> None:
            assert standalone_tool_id == "file-sorter"
            self.safe_initializations += 1

    class FakeAutomation:
        def __init__(
            self,
            project_root: Path,
            logger: Any,
            *,
            allowed_tool_ids: set[str],
        ) -> None:
            self.project_root = project_root
            self.logger = logger
            self.allowed_tool_ids = allowed_tool_ids
            self.starts = 0

        async def start(self) -> bool:
            self.starts += 1
            return True

        async def stop(self) -> None:
            return None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("main initialization escaped standalone scope")

    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("GPTBRIDGE_STANDALONE_TOOL_ID", "file-sorter")
    monkeypatch.setattr(main_module, "load_config", forbidden)
    monkeypatch.setattr(main_module, "ensure_backup_layout", forbidden)
    monkeypatch.setattr(main_module, "PlatformAutomationManager", FakeAutomation)

    tool_root = tmp_path / "platform_tools" / "file-sorter"
    tool_root.mkdir(parents=True)
    (tool_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "file-sorter",
                "version": "4.0.0",
                "runtime": {"entry": "src/main.py"},
            }
        ),
        encoding="utf-8",
    )
    app = main_module.GPTBridgeApp()
    mode_manager = FakeModeManager()
    app.mode_manager = mode_manager  # type: ignore[assignment]
    app._manage_startup_entry = forbidden  # type: ignore[method-assign]

    await app.initialize_standalone_tool()

    assert mode_manager.safe_initializations == 1
    assert app.platform_automation is not None
    assert app.platform_automation.allowed_tool_ids == {"file-sorter"}
    assert app.platform_automation.starts == 1
    assert app.backup_manager is None
    assert app.toolbox_service is not None
    assert app.toolbox_service.allowed_tool_ids == frozenset({"file-sorter"})
    assert app.standalone_capabilities["tool_id"] == "file-sorter"
    assert app.standalone_capabilities["tool_version"] == "4.0.0"
    assert app.session is None


@pytest.mark.asyncio
@pytest.mark.skip(reason="Mode-service shutdown was removed from the v1.0 main process")
async def test_app_shutdown_is_idempotent_for_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Service:
        shutdown_calls = 0

        async def shutdown(self) -> None:
            self.shutdown_calls += 1
            await asyncio.sleep(0)

    service = Service()

    class FakeModeManager:
        def all_mode_services(self) -> list[Any]:
            return [service]

    monkeypatch.setenv("GPTBRIDGE_PROJECT_ROOT", str(tmp_path))
    app = main_module.GPTBridgeApp()
    app.mode_manager = FakeModeManager()  # type: ignore[assignment]

    await asyncio.gather(app.shutdown(), app.shutdown())
    await app.shutdown()

    assert service.shutdown_calls == 1


@pytest.mark.asyncio
async def test_platform_automation_manager_loads_manifest_declared_service(
    tmp_path: Path,
) -> None:
    tool_dir = tmp_path / "platform_tools" / "sample-tool"
    service_path = tool_dir / "src" / "automation.py"
    service_path.parent.mkdir(parents=True)
    service_path.write_text(
        "\n".join(
            [
                "class DemoAutomation:",
                "    def __init__(self, project_root, logger):",
                "        self.project_root = project_root",
                "        self.logger = logger",
                "        self.starts = 0",
                "        self.stops = 0",
                "    async def start(self):",
                "        self.starts += 1",
                "        return True",
                "    async def stop(self):",
                "        self.stops += 1",
            ]
        ),
        encoding="utf-8",
    )
    (tool_dir / "manifest.json").write_text(
        '{"id":"sample-tool","automation":'
        '{"entry":"src/automation.py","class":"DemoAutomation"}}',
        encoding="utf-8",
    )
    manager = PlatformAutomationManager(tmp_path)

    assert await manager.start() is True
    service = manager._services["sample-tool"]
    assert service.starts == 1
    assert service.project_root == tmp_path.resolve()

    await manager.stop()
    assert service.stops == 1
    assert manager._services == {}
