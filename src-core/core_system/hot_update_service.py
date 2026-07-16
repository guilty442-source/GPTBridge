from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any


class HotUpdateService:
    """Coordinate update repair and scoped independent-tool restarts."""

    def __init__(self, app: Any, interval_seconds: float = 1.0) -> None:
        self.app = app
        self.interval_seconds = max(0.5, interval_seconds)
        self._task: asyncio.Task[Any] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch(), name="hot-update-coordinator")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            plan = self.app.update_coordinator.inspect()
            if not plan.get("changed"):
                continue
            try:
                await self._apply(plan)
            except Exception as error:
                self.app._log(
                    {
                        "type": "hot_update_failed",
                        "error": type(error).__name__,
                    }
                )

    async def _apply(
        self,
        plan: dict[str, Any],
        *,
        repairs_completed: bool = False,
    ) -> None:
        changes = plan.get("changes") or []
        core_changed = any(
            str(change.get("path") or "").startswith(("src-core/", "src-ui/main/"))
            for change in changes
            if isinstance(change, dict)
        )
        if not repairs_completed:
            repairs_ok = await self.app._run_declared_auto_repairs(
                include_upgrade_repair=core_changed
            )
            if not repairs_ok:
                return

        affected_tools = {
            parts[1]
            for change in changes
            if isinstance(change, dict)
            for path in [str(change.get("path") or "")]
            for parts in [path.split("/")]
            if len(parts) > 2 and parts[0] == "platform_tools"
        }
        service = self.app.toolbox_service
        synchronized_tools: list[str] = []
        renderer_tools: list[str] = []
        if service is not None and affected_tools:
            listed = await service.list_tools()
            running = {
                str(tool.get("id") or "")
                for tool in listed.get("tools", [])
                if isinstance(tool, dict) and tool.get("status") == "running"
            }
            for tool_id in sorted(affected_tools):
                tool_changes = [
                    str(change.get("path") or "")
                    for change in changes
                    if isinstance(change, dict)
                    and str(change.get("path") or "").startswith(
                        f"platform_tools/{tool_id}/"
                    )
                ]
                was_running = tool_id in running
                request_id = f"hot-update:{uuid.uuid4().hex}"
                if was_running:
                    stopped = await service.stop_tool(
                        {"tool_id": tool_id, "request_id": request_id}
                    )
                    if not stopped.get("ok"):
                        raise RuntimeError(f"failed to stop {tool_id} for hot update")
                backend_stopped = await service.shutdown_tool_backend(
                    tool_id,
                    reason="hot-reload",
                )
                if not backend_stopped.get("ok"):
                    raise RuntimeError(
                        f"failed to stop {tool_id} backend for hot update"
                    )
                synchronized = await asyncio.to_thread(
                    self._synchronize_tool_source,
                    tool_id,
                )
                if synchronized:
                    synchronized_tools.append(tool_id)
                if self._requires_renderer_build(tool_id, tool_changes):
                    await self._build_renderer(tool_id)
                    await asyncio.to_thread(self._install_renderer_overlay, tool_id)
                    renderer_tools.append(tool_id)
                if was_running:
                    started = await service.start_tool(
                        {"tool_id": tool_id, "request_id": request_id}
                    )
                    if not started.get("ok"):
                        raise RuntimeError(f"failed to restart {tool_id} after hot update")

        self.app.update_coordinator.mark_applied()
        self.app._log(
            {
                "type": "hot_update_applied",
                "changed_count": int(plan.get("changed_count") or 0),
                "affected_tools": sorted(affected_tools),
                "source_synchronized_tools": synchronized_tools,
                "renderer_overlay_tools": renderer_tools,
            }
        )

    def _standalone_tool_root(self, tool_id: str) -> Path:
        state_base = Path(
            str(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        )
        return (state_base / "GPTBridge" / "standalone" / tool_id).resolve()

    @staticmethod
    def _assert_inside(root: Path, candidate: Path) -> None:
        candidate.resolve(strict=False).relative_to(root.resolve())

    @classmethod
    def _validate_source_tree(cls, root: Path) -> None:
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise RuntimeError(f"hot update source contains a link: {candidate}")

    def _synchronize_tool_source(self, tool_id: str) -> bool:
        source_root = (
            self.app.project_root / "platform_tools" / tool_id
        ).resolve()
        source_tree = source_root / "src"
        source_manifest = source_root / "manifest.json"
        if not source_tree.is_dir() or not source_manifest.is_file():
            raise RuntimeError(f"hot update source is incomplete for {tool_id}")
        self._validate_source_tree(source_tree)
        standalone_root = self._standalone_tool_root(tool_id)
        if not standalone_root.is_dir():
            return False
        installed_tool_root = standalone_root / "platform_tools" / tool_id
        installed_source = installed_tool_root / "src"
        operation_id = uuid.uuid4().hex
        staging_root = installed_tool_root / f".hot-update-staging-{operation_id}"
        staged_source = staging_root / "src"
        backup_source = installed_tool_root / f".hot-update-backup-{operation_id}"
        for candidate in (installed_tool_root, installed_source, staging_root, backup_source):
            self._assert_inside(standalone_root, candidate)
        installed_tool_root.mkdir(parents=True, exist_ok=True)
        moved_original = False
        installed_new = False
        try:
            shutil.copytree(source_tree, staged_source)
            self._validate_source_tree(staged_source)
            if installed_source.exists():
                installed_source.rename(backup_source)
                moved_original = True
            staged_source.rename(installed_source)
            installed_new = True
            manifest_temp = installed_tool_root / f"manifest.{operation_id}.tmp"
            shutil.copy2(source_manifest, manifest_temp)
            os.replace(manifest_temp, installed_tool_root / "manifest.json")
            if backup_source.exists():
                shutil.rmtree(backup_source)
            return True
        except Exception:
            if installed_new and installed_source.exists():
                shutil.rmtree(installed_source)
            if moved_original and backup_source.exists():
                backup_source.rename(installed_source)
            raise
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)

    @staticmethod
    def _requires_renderer_build(tool_id: str, changes: list[str]) -> bool:
        prefix = f"platform_tools/{tool_id}/src/ui/"
        return any(
            path.startswith(prefix)
            or path.endswith((".tsx", ".ts", ".css", ".scss", ".html"))
            for path in changes
        )

    async def _build_renderer(self, tool_id: str) -> None:
        project_root = Path(self.app.project_root).resolve()
        bundled_python = project_root / ".venv" / "Scripts" / "python.exe"
        python_executable = bundled_python if bundled_python.is_file() else Path(sys.executable)
        process = await asyncio.create_subprocess_exec(
            str(python_executable),
            str(project_root / "scripts" / "package_platform_tools.py"),
            "--build-renderers-only",
            tool_id,
            cwd=str(project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0:
            message = output.decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"renderer hot build failed for {tool_id}: {message}")

    @staticmethod
    def _file_digests(root: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise RuntimeError(f"renderer hot update contains a link: {candidate}")
            if not candidate.is_file() or candidate.name == "update.json":
                continue
            relative = candidate.relative_to(root).as_posix()
            files[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return files

    def _install_renderer_overlay(self, tool_id: str) -> None:
        project_root = Path(self.app.project_root).resolve()
        built_renderer = (
            project_root / "dist-ui" / "platform-tools" / tool_id / "renderer"
        )
        if not (built_renderer / "index.html").is_file():
            raise RuntimeError(f"renderer hot build output is missing for {tool_id}")
        standalone_root = self._standalone_tool_root(tool_id)
        if not standalone_root.is_dir():
            return
        hot_root = standalone_root / "runtime" / "hot-update"
        renderer_root = hot_root / "renderer"
        operation_id = uuid.uuid4().hex
        staging_root = hot_root / f".renderer-staging-{operation_id}"
        backup_root = hot_root / f".renderer-backup-{operation_id}"
        for candidate in (hot_root, renderer_root, staging_root, backup_root):
            self._assert_inside(standalone_root, candidate)
        hot_root.mkdir(parents=True, exist_ok=True)
        moved_original = False
        installed_new = False
        try:
            shutil.copytree(built_renderer, staging_root)
            files = self._file_digests(staging_root)
            manifest = json.loads(
                (
                    project_root / "platform_tools" / tool_id / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            (staging_root / "update.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "tool_id": tool_id,
                        "tool_version": str(manifest.get("version") or ""),
                        "files": files,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if renderer_root.exists():
                renderer_root.rename(backup_root)
                moved_original = True
            staging_root.rename(renderer_root)
            installed_new = True
            if backup_root.exists():
                shutil.rmtree(backup_root)
        except Exception:
            if installed_new and renderer_root.exists():
                shutil.rmtree(renderer_root)
            if moved_original and backup_root.exists():
                backup_root.rename(renderer_root)
            raise
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)
