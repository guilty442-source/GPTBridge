from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


class PlatformAutomationManager:
    """Load explicitly declared background services from platform manifests."""

    def __init__(
        self,
        project_root: str | Path,
        logger: Any | None = None,
        *,
        allowed_tool_ids: set[str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.logger = logger
        self.tools_root = self.project_root / "platform_tools"
        self.allowed_tool_ids = (
            frozenset(allowed_tool_ids)
            if allowed_tool_ids is not None
            else None
        )
        self._services: dict[str, Any] = {}

    @staticmethod
    def _is_within(candidate: Path, parent: Path) -> bool:
        try:
            candidate.relative_to(parent)
        except ValueError:
            return False
        return True

    def _automation_declarations(self) -> list[tuple[str, Path, str]]:
        declarations: list[tuple[str, Path, str]] = []
        if not self.tools_root.is_dir():
            return declarations

        for tool_dir in sorted(
            self.tools_root.iterdir(),
            key=lambda item: item.name.casefold(),
        ):
            manifest_path = tool_dir / "manifest.json"
            if not tool_dir.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            automation = manifest.get("automation")
            if not isinstance(automation, dict) or automation.get("enabled", True) is False:
                continue
            entry = str(automation.get("entry", "")).strip()
            class_name = str(automation.get("class", "")).strip()
            tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
            if (
                self.allowed_tool_ids is not None
                and tool_id not in self.allowed_tool_ids
            ):
                continue
            if not entry or not class_name or not class_name.isidentifier():
                continue

            tool_root = tool_dir.resolve()
            entry_path = (tool_root / entry).resolve()
            if (
                not self._is_within(entry_path, tool_root)
                or not entry_path.is_file()
                or entry_path.suffix.lower() != ".py"
            ):
                continue
            declarations.append((tool_id, entry_path, class_name))
        return declarations

    def _load_service(self, tool_id: str, entry: Path, class_name: str) -> Any:
        module_key = hashlib.sha256(
            str(entry).encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:16]
        module_name = f"_gptbridge_platform_automation_{module_key}"
        module = sys.modules.get(module_name)
        if not isinstance(module, ModuleType):
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise ImportError("Platform automation module could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise

        service_class = getattr(module, class_name, None)
        if not isinstance(service_class, type):
            raise ImportError("Platform automation class is unavailable")
        service = service_class(self.project_root, self.logger)
        if not callable(getattr(service, "start", None)) or not callable(
            getattr(service, "stop", None)
        ):
            raise TypeError("Platform automation service requires start() and stop()")
        return service

    async def start(self) -> bool:
        started_any = False
        for tool_id, entry, class_name in self._automation_declarations():
            service = self._services.get(tool_id)
            if service is None:
                try:
                    service = self._load_service(tool_id, entry, class_name)
                except Exception as error:
                    self._safe_log(
                        "error",
                        "Platform automation service could not be loaded.",
                        {"tool_id": tool_id, "error_type": type(error).__name__},
                    )
                    continue
                self._services[tool_id] = service
            try:
                result = service.start()
                if inspect.isawaitable(result):
                    result = await result
                started_any = bool(result) or started_any
            except Exception as error:
                self._safe_log(
                    "error",
                    "Platform automation service could not be started.",
                    {"tool_id": tool_id, "error_type": type(error).__name__},
                )
        return started_any

    async def stop(self) -> None:
        services = list(reversed(self._services.items()))
        self._services = {}
        for tool_id, service in services:
            try:
                result = service.stop()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                self._safe_log(
                    "error",
                    "Platform automation service could not be stopped cleanly.",
                    {"tool_id": tool_id, "error_type": type(error).__name__},
                )

    def _safe_log(
        self,
        level: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        if self.logger is None:
            return
        try:
            write = getattr(self.logger, "write", None)
            if callable(write):
                write("platform-automation", message, {"level": level, **payload})
        except Exception:
            return
