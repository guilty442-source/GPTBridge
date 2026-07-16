from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC_CORE = ROOT / "src-core"
if str(SRC_CORE) not in sys.path:
    sys.path.insert(0, str(SRC_CORE))

from core_system.hot_update_service import HotUpdateService
from main import GPTBridgeApp
from tasks.toolbox_service import ToolboxService


def _service(project_root: Path) -> HotUpdateService:
    app = SimpleNamespace(project_root=project_root)
    return HotUpdateService(app)


def test_backend_hot_update_replaces_only_installed_tool_source(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    source_root = project_root / "platform_tools" / "demo-tool"
    (source_root / "src" / "backend").mkdir(parents=True)
    (source_root / "src" / "backend" / "service.py").write_text(
        "VALUE = 'new'\n", encoding="utf-8"
    )
    (source_root / "manifest.json").write_text(
        json.dumps({"id": "demo-tool", "version": "1.0.0"}), encoding="utf-8"
    )
    local_data = tmp_path / "local-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    installed = (
        local_data
        / "GPTBridge"
        / "standalone"
        / "demo-tool"
        / "platform_tools"
        / "demo-tool"
    )
    (installed / "src" / "backend").mkdir(parents=True)
    (installed / "src" / "backend" / "service.py").write_text(
        "VALUE = 'old'\n", encoding="utf-8"
    )
    (installed / "src" / "obsolete.py").write_text("old\n", encoding="utf-8")

    changed = _service(project_root)._synchronize_tool_source("demo-tool")

    assert changed is True
    assert (installed / "src" / "backend" / "service.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 'new'\n"
    assert not (installed / "src" / "obsolete.py").exists()
    assert not list(installed.glob(".hot-update-*"))


def test_frontend_hot_update_installs_integrity_checked_renderer_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    tool_root = project_root / "platform_tools" / "demo-tool"
    tool_root.mkdir(parents=True)
    (tool_root / "manifest.json").write_text(
        json.dumps({"id": "demo-tool", "version": "1.0.0"}), encoding="utf-8"
    )
    renderer = project_root / "dist-ui" / "platform-tools" / "demo-tool" / "renderer"
    (renderer / "assets").mkdir(parents=True)
    (renderer / "index.html").write_text("<main>new</main>", encoding="utf-8")
    (renderer / "assets" / "app.js").write_text("window.ready=true\n", encoding="utf-8")
    local_data = tmp_path / "local-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))
    standalone = local_data / "GPTBridge" / "standalone" / "demo-tool"
    standalone.mkdir(parents=True)

    _service(project_root)._install_renderer_overlay("demo-tool")

    installed = standalone / "runtime" / "hot-update" / "renderer"
    marker = json.loads((installed / "update.json").read_text(encoding="utf-8"))
    assert marker["tool_id"] == "demo-tool"
    assert marker["tool_version"] == "1.0.0"
    assert set(marker["files"]) == {"assets/app.js", "index.html"}
    assert not list((standalone / "runtime" / "hot-update").glob(".*-backup-*"))


def test_tool_renderer_template_prefers_valid_local_hot_update_overlay() -> None:
    template = (
        ROOT / "scripts" / "templates" / "platform-tool-app" / "main.cjs"
    ).read_text(encoding="utf-8")
    assert "resolveHotUpdateRenderer() || packagedRendererPath" in template
    assert "renderer.hotUpdateSelected" in template
    assert "createHash('sha256')" in template


def test_renderer_build_is_required_only_for_frontend_changes() -> None:
    service = _service(ROOT)
    assert service._requires_renderer_build(
        "local-ai", ["platform_tools/local-ai/src/ui/LocalAiWindowApp.tsx"]
    )
    assert not service._requires_renderer_build(
        "local-ai", ["platform_tools/local-ai/src/backend/services/local_ai/service.py"]
    )


@pytest.mark.asyncio
async def test_hot_update_stops_backend_before_sync_and_restart(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []

    class Toolbox:
        async def list_tools(self):
            return {"tools": [{"id": "demo-tool", "status": "running"}]}

        async def stop_tool(self, _payload):
            events.append("stop-executable")
            return {"ok": True}

        async def shutdown_tool_backend(self, tool_id, *, reason):
            assert tool_id == "demo-tool"
            assert reason == "hot-reload"
            events.append("stop-backend")
            return {"ok": True}

        async def start_tool(self, _payload):
            events.append("start")
            return {"ok": True}

    class Coordinator:
        def mark_applied(self):
            events.append("mark-applied")

    async def repairs(*, include_upgrade_repair):
        assert include_upgrade_repair is False
        return True

    project_root = tmp_path / "project"
    source = project_root / "platform_tools" / "demo-tool" / "src"
    source.mkdir(parents=True)
    (source / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source.parent / "manifest.json").write_text(
        json.dumps({"id": "demo-tool", "version": "1.0.0"}), encoding="utf-8"
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-data"))
    app = SimpleNamespace(
        project_root=project_root,
        toolbox_service=Toolbox(),
        update_coordinator=Coordinator(),
        _run_declared_auto_repairs=repairs,
        _log=lambda _event: None,
    )
    service = HotUpdateService(app)
    original_sync = service._synchronize_tool_source

    def tracked_sync(tool_id: str) -> bool:
        events.append("sync")
        return original_sync(tool_id)

    monkeypatch.setattr(service, "_synchronize_tool_source", tracked_sync)
    await service._apply(
        {
            "changed_count": 1,
            "changes": [
                {"path": "platform_tools/demo-tool/src/backend/service.py"}
            ],
        }
    )

    assert events == [
        "stop-executable",
        "stop-backend",
        "sync",
        "start",
        "mark-applied",
    ]


@pytest.mark.asyncio
async def test_tool_local_auto_repairs_run_concurrently(tmp_path: Path) -> None:
    for tool_id in ("alpha-tool", "beta-tool"):
        tool_root = tmp_path / "platform_tools" / tool_id
        (tool_root / "src").mkdir(parents=True)
        (tool_root / "src" / "auto_repair.py").write_text(
            "import time\ntime.sleep(0.5)\nprint('ok')\n",
            encoding="utf-8",
        )
        (tool_root / "manifest.json").write_text(
            json.dumps(
                {
                    "id": tool_id,
                    "enabled": True,
                    "capabilities": {
                        "auto-repair": {
                            "entry": "src/auto_repair.py",
                            "arguments": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    records: list[tuple[str, bool, str]] = []
    app = GPTBridgeApp.__new__(GPTBridgeApp)
    app.project_root = tmp_path
    app.toolbox_service = ToolboxService(tmp_path)
    app.update_coordinator = SimpleNamespace(
        repository=SimpleNamespace(
            record_repair=lambda name, ok, message: records.append(
                (name, ok, message)
            )
        )
    )
    app._log = lambda _event: None

    started = time.monotonic()
    result = await app._run_declared_auto_repairs(include_upgrade_repair=False)
    elapsed = time.monotonic() - started

    assert result is True
    assert elapsed < 0.9
    assert {name for name, ok, _message in records if ok} == {
        "alpha-tool:auto-repair",
        "beta-tool:auto-repair",
    }


@pytest.mark.asyncio
async def test_startup_maintenance_applies_pending_hot_update_before_ready() -> None:
    events: list[object] = []

    class HotUpdater:
        async def _apply(self, plan, *, repairs_completed=False):
            events.append(("apply", plan["changed_count"], repairs_completed))

        def start(self):
            events.append("watcher-start")

    async def repairs(*, include_upgrade_repair):
        events.append(("repair", include_upgrade_repair))
        return True

    async def background_tools():
        events.append("background-tools")

    app = GPTBridgeApp.__new__(GPTBridgeApp)
    app.maintenance_ready = False
    app.hot_update_service = HotUpdater()
    app._run_declared_auto_repairs = repairs
    app._independent_tool_startup_task = None
    app._start_manifest_background_tools = background_tools
    app._log = lambda event: events.append(event["type"])
    app.update_coordinator = SimpleNamespace(
        mark_applied=lambda: events.append("mark-applied")
    )

    await app._complete_startup_maintenance(
        update_plan={"changed": True, "changed_count": 2, "changes": []}
    )
    await asyncio.sleep(0)

    assert app.maintenance_ready is True
    assert ("repair", True) in events
    assert ("apply", 2, True) in events
    assert "mark-applied" not in events
    assert "watcher-start" in events
