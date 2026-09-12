from __future__ import annotations

import asyncio
import threading
from typing import Any

from ..integration.channel_client import build_star_ai_channel_client


_GIT_COMMANDS = frozenset(
    {
        "xingcheng_git_status",
        "xingcheng_git_history",
        "xingcheng_git_stage",
        "xingcheng_git_commit",
    }
)
_RAG_COMMANDS = frozenset(
    {
        "xingcheng_rag_status",
        "xingcheng_rag_ingest",
        "xingcheng_rag_query",
        "xingcheng_knowledge_unified_search",
    }
)
_SQL_COMMANDS = frozenset(
    {
        "xingcheng_sql_status",
        "xingcheng_sql_get_personality",
        "xingcheng_sql_save_personality",
        "xingcheng_sql_list_knowledge",
        "xingcheng_sql_save_knowledge",
    }
)
_MEMORY_UPGRADE_COMMANDS = frozenset(
    {
        "xingcheng_evaluate_upgrade",
        "xingcheng_memory_list",
        "xingcheng_memory_review",
    }
)
_TUNE_MOBILE_COMMANDS = frozenset(
    {
        "xingcheng_tune_investment_parameters",
        "xingcheng_mobile_get_investment_snapshot",
        "xingcheng_mobile_submit_investment_instruction",
    }
)
_INVESTMENT_COMMANDS = frozenset(
    {
        "xingcheng_search_investments",
        "xingcheng_analyze_investments",
        "xingcheng_discuss_investment_analysis",
        "xingcheng_manage_investment_accounting",
    }
)
_DIAGNOSTICS_COMMANDS = frozenset(
    {
        "xingcheng_diagnose_fault",
    }
)


class LocalAiLifecycleMixin:
    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    def bind_channel(self, channel: Any) -> None:
        self._ai_channel_client = build_star_ai_channel_client(channel)

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
        if command in _GIT_COMMANDS:
            return await self._handle_git(command, payload)
        if command == "xingcheng_platform_status":
            return await self._handle_platform(command, payload)
        if command in _RAG_COMMANDS:
            return await self._handle_rag(command, payload)
        if command in _SQL_COMMANDS:
            return await self._handle_sql(command, payload)
        if command == "xingcheng_status":
            return await self._handle_status(command, payload)
        if command in _MEMORY_UPGRADE_COMMANDS:
            return await self._handle_upgrade_memory(command, payload)
        if command in _TUNE_MOBILE_COMMANDS:
            return await self._handle_tune_mobile(command, payload)
        if command in _INVESTMENT_COMMANDS:
            return await self._handle_investments(command, payload)
        if command in _DIAGNOSTICS_COMMANDS:
            return await self._handle_diagnostics(command, payload)
        return await self._handle_infer(command, payload)
