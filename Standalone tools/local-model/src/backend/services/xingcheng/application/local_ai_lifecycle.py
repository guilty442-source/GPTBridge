from __future__ import annotations

import asyncio
import threading
from typing import Any

from ..integration.channel_client import build_star_ai_channel_client
from .xingcheng_commands import get_xingcheng_registry, resolve_command
from .command_parser import validate_parameters


class LocalAiLifecycleMixin:
    def owns(self, command: str) -> bool:
        registry = get_xingcheng_registry()
        return resolve_command(command) is not None

    def bind_channel(self, channel: Any) -> None:
        self._ai_channel_client = build_star_ai_channel_client(channel)
        self.external_research.bind_channel(channel)

    def begin_request(self, request_id: str) -> threading.Event:
        event = threading.Event()
        self._request_cancel_events[str(request_id)] = event
        return event

    def finish_request(self, request_id: str) -> None:
        self._request_cancel_events.pop(str(request_id), None)

    async def cancel_request(self, request_id: str) -> bool:
        event = self._request_cancel_events.get(str(request_id))
        if event is None:
            return False
        event.set()
        return True

    async def start(self) -> None:
        await asyncio.gather(
            asyncio.to_thread(self._run_self_maintenance),
            asyncio.to_thread(self.transformer_runtime.probe),
        )
        if (
            self.transformer_runtime.enabled
            and self._internal_maintenance_loop_task is None
        ):
            self._default_model_preload_task = asyncio.create_task(
                asyncio.to_thread(
                    self.transformer_runtime.resource_manager.preload_model,
                    self.transformer_runtime.MODEL,
                    keep_alive=-1,
                )
            )
            self._internal_maintenance_loop_task = asyncio.create_task(
                self._internal_maintenance_loop()
            )

    async def shutdown(self) -> None:
        preload = self._default_model_preload_task
        self._default_model_preload_task = None
        if preload is not None:
            await asyncio.gather(preload, return_exceptions=True)
        task = self._internal_maintenance_loop_task
        self._internal_maintenance_loop_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        training = self._internal_training_task
        self._internal_training_task = None
        if training is not None and not training.done():
            training.cancel()
            await asyncio.gather(training, return_exceptions=True)

    async def _internal_maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self._request_cancel_events or not self._internal_training_due():
                continue
            self._internal_training_task = asyncio.create_task(
                self._run_internal_ollama_training()
            )
            await asyncio.gather(
                self._internal_training_task,
                return_exceptions=True,
            )

    async def handle(self, command: str, payload: dict[str, Any], _latest: Any = None
    ) -> tuple[str, dict[str, Any]]:
        """Handle incoming command using the new command registry."""
        # Resolve command with fuzzy matching
        spec = resolve_command(command)

        if spec is None:
            from .xingcheng_commands import get_xingcheng_registry
            registry = get_xingcheng_registry()
            # The registry may have been empty on first use; resolve again
            # once it has been populated.
            spec = resolve_command(command)
        if spec is None:
            suggestions = registry.get_suggestions(command)
            if suggestions:
                return "error", {
                    "ok": False,
                    "error_code": "UNKNOWN_COMMAND",
                    "message": f"未知命令: {command}",
                    "suggestions": suggestions,
                }
            return "error", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": f"未知命令: {command}",
            }

        # Dispatch to appropriate handler based on command name
        handler_name = spec.handler
        handler = getattr(self, handler_name, None)

        if handler is None:
            return "error", {
                "ok": False,
                "error_code": "HANDLER_NOT_FOUND",
                "message": f"命令處理器未找到: {spec.handler}",
            }

        # Validate parameters if spec has parameters
        if spec.parameters:
            from .command_parser import validate_parameters
            try:
                validated_payload = validate_parameters(payload, spec)
                payload = validated_payload
            except Exception as e:
                return "error", {
                    "ok": False,
                    "error_code": "PARAM_VALIDATION_ERROR",
                    "message": str(e),
                }

        # Call the handler
        return await handler(command, payload)
