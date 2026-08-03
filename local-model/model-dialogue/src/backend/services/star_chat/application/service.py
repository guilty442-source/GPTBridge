from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable


class StarChatService:
    """A separated, governed client for local model conversation."""

    VERSION = "1.0.0"
    PRIMARY_LANGUAGE = "zh-TW"
    AUTOMATIC_WORKFLOW_SEQUENCE = (
        "receive-original-traditional-chinese",
        "qwen3.8-understand-command-and-normalize-taiwan-chinese",
        "rnj-1-analyze-code-stem-and-tool-calling-at-workflow-front",
        "extract-actions-objects-parameters-constraints",
        "classify-task-and-intensity",
        "apply-safety-and-permission-gates",
        "decompose-and-route-subtasks",
        "execute-and-collect-results",
        "cross-validate-repair-or-escalate",
        "integrate-localize-apply-verify-and-report",
    )
    REASONING_LEVEL_TO_EFFORT = {
        "light": "low",
        "intermediate": "medium",
        "high-high": "high",
        "ultra-high": "high",
        "extreme": "high",
    }
    GENERATION_SPEED_MULTIPLIER = {
        "slow": 1.25,
        "low": 1.1,
        "medium": 1.0,
        "high": 0.75,
        "ultra": 0.5,
    }
    TASK_INTENSITY_SETTINGS = {
        "simple": {
            "base_output_tokens": 256,
            "reasoning_path": "single-model-fast-path",
            "model_selection_strategy": "fast-local-preference",
        },
        "normal": {
            "base_output_tokens": 512,
            "reasoning_path": "balanced-specialist-path",
            "model_selection_strategy": "balanced-intent-routing",
        },
        "intermediate": {
            "base_output_tokens": 768,
            "reasoning_path": "specialist-reasoning-path",
            "model_selection_strategy": "intent-specialist-routing",
        },
        "difficult": {
            "base_output_tokens": 1_024,
            "reasoning_path": "full-governed-multistage-path",
            "model_selection_strategy": "full-pipeline-routing",
        },
    }
    CONVERSATION_MODES = frozenset({"chat", "coding"})
    MODE_INSTRUCTIONS = {
        "chat": (
            "目前是 Chat 模式。以自然、友善、直接的對話方式回應；專注問答、"
            "討論、解釋與內容協作，不假設使用者要求操作程式專案。"
        ),
        "coding": (
            "目前是 Coding 模式。以軟體工程師方式處理需求，優先提供可執行、"
            "可驗證的程式方案，並結合已選擇的編程資料夾脈絡。"
        ),
    }
    COMMANDS = frozenset(
        {
            "star_chat_status",
            "star_chat_send_message",
        }
    )
    def __init__(self) -> None:
        self._client: Any | None = None
        self._local_service: Any | None = None
        self._active_nested_requests: dict[str, str] = {}

    def bind_channel(self, channel: Any) -> None:
        from governance_rule.permission_directory.registries.permissions.tool_routes import (
            authorize_ai_route,
            tool_actor,
        )
        from shared_layer import GovernedRequestClient

        self._client = GovernedRequestClient(
            channel,
            tool_actor("star-chat"),
            authorize_ai_route,
            transport="governance-authenticated-ai-channel",
        )

    def bind_local_service(self, service: Any) -> None:
        """Bind the dialogue facade directly to its physical owner runtime."""

        self._local_service = service

    @property
    def connected(self) -> bool:
        return self._client is not None or self._local_service is not None

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    @staticmethod
    def _bounded_text(value: Any, maximum: int) -> str:
        return str(value or "").strip()[:maximum]

    @classmethod
    def _conversation_prompt(cls, payload: dict[str, Any]) -> str:
        message = cls._bounded_text(payload.get("message") or payload.get("prompt"), 32_000)
        mode = cls._conversation_mode(payload)
        instruction = cls.MODE_INSTRUCTIONS[mode]
        previous_mode = cls._bounded_text(
            payload.get("previous_conversation_mode"), 16
        ).casefold()
        transition = ""
        if previous_mode in cls.CONVERSATION_MODES and previous_mode != mode:
            transition = (
                f"模式切換通知：使用者已從 {previous_mode.title()} 切換至 "
                f"{mode.title()}。請先完成角色與處理策略切換，再回應最新訊息。\n\n"
            )
        context = cls._conversation_context(payload)
        if not context:
            return f"{transition}{instruction}\n\n使用者最新訊息：{message}"
        return f"{transition}{instruction}\n\n以下是同一段對話的最近內容：\n{context}\n\n使用者最新訊息：{message}"

    @classmethod
    def _conversation_mode(cls, payload: dict[str, Any]) -> str:
        mode = cls._bounded_text(payload.get("conversation_mode"), 16).casefold()
        return mode if mode in cls.CONVERSATION_MODES else "chat"

    @staticmethod
    def _context_budget(payload: dict[str, Any]) -> int:
        try:
            requested = int(payload.get("context_budget_characters") or 20_000)
        except (TypeError, ValueError):
            requested = 20_000
        return max(4_000, min(24_000, requested))

    @classmethod
    def _conversation_context(cls, payload: dict[str, Any]) -> str:
        history = payload.get("history")
        context_budget = cls._context_budget(payload)
        rows: list[str] = []
        if isinstance(history, list):
            for item in history[-12:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().casefold()
                if role not in {"user", "assistant"}:
                    continue
                content = cls._bounded_text(item.get("content"), 4_000)
                if content:
                    rows.append(f"{'使用者' if role == 'user' else '星澄'}：{content}")
        return "\n".join(rows)[-context_budget:]

    async def _request(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        parent_request_id: str = "",
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        if self._local_service is not None:
            local_payload = dict(payload)
            if progress_callback is not None:
                local_payload["_progress_callback"] = progress_callback
            if cancel_event is not None:
                local_payload["_cancel_event"] = cancel_event
            _event, result = await self._local_service.handle(command, local_payload)
            if not isinstance(result, dict):
                raise PermissionError("PERMISSION_DENIED")
            return result
        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄 AI 通道尚未連線。",
            }
        nested_request_id = (
            f"star-nested-{uuid.uuid4().hex}" if parent_request_id else None
        )
        if parent_request_id and nested_request_id:
            self._active_nested_requests[parent_request_id] = nested_request_id
        try:
            if nested_request_id:
                return await self._client.request(
                    "local-ai",
                    command,
                    payload,
                    timeout_seconds=timeout_seconds,
                    request_id=nested_request_id,
                    progress_callback=progress_callback,
                )
            return await self._client.request(
                "local-ai", command, payload, timeout_seconds=timeout_seconds
            )
        finally:
            if (
                parent_request_id
                and self._active_nested_requests.get(parent_request_id)
                == nested_request_id
            ):
                self._active_nested_requests.pop(parent_request_id, None)

    async def cancel_request(self, parent_request_id: str) -> bool:
        if self._local_service is not None:
            return bool(await self._local_service.cancel_request(parent_request_id))
        nested_request_id = self._active_nested_requests.get(
            str(parent_request_id or "").strip()
        )
        if self._client is None or not nested_request_id:
            return False
        return await asyncio.to_thread(
            self._client.cancel, "local-ai", nested_request_id
        )

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        request_id: str = "",
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if command == "star_chat_status":
            prepare_mode = self._bounded_text(
                payload.get("prepare_mode"), 16
            ).casefold()
            result = await self._request(
                "local_ai_status",
                {
                    "prepare_mode": prepare_mode
                    if prepare_mode in self.CONVERSATION_MODES
                    else ""
                },
                timeout_seconds=120,
                parent_request_id=request_id,
                cancel_event=payload.get("_cancel_event"),
            )
            return f"{command}_result", {
                **result,
                "client_version": self.VERSION,
                "client_tool": "star-chat",
                "main_system_independent_tool": True,
                "independent_only_in": "main-system",
                "model_service_owner": "local-ai",
                "settings_owner": "local-ai",
                "business_layer_owner": "local-ai",
                "permission_profile": "local-model-platform-v1",
                "cache_owner": "local-ai",
                "cache_storage": "local-model/runtime/cache/companions/star-chat",
                "backup_owner": "local-ai",
                "backup_storage": "global-cleaner/data/business/backups/local-ai",
                "separated_from_model_service": False,
                "database_shared": True,
                "separate_business_layer": False,
                "separate_settings_layer": False,
                "channel_workflow": {
                    "automatic": True,
                    "primary_language": self.PRIMARY_LANGUAGE,
                    "sequence": list(self.AUTOMATIC_WORKFLOW_SEQUENCE),
                },
            }
        if command == "star_chat_send_message":
            conversation_mode = self._conversation_mode(payload)
            raw_message = self._bounded_text(
                payload.get("message") or payload.get("prompt"),
                32_000,
            )
            command_context = self._conversation_context(payload)
            prompt = self._conversation_prompt(payload)
            if not prompt:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "MESSAGE_REQUIRED",
                    "message": "請輸入要對星澄說的內容。",
                }
            runtime_model = self._bounded_text(payload.get("runtime_model"), 256)
            reasoning_level = self._bounded_text(
                payload.get("reasoning_level"), 32
            ).casefold()
            reasoning_effort = self._bounded_text(
                payload.get("reasoning_effort"), 16
            ).casefold()
            if reasoning_level in self.REASONING_LEVEL_TO_EFFORT:
                reasoning_effort = self.REASONING_LEVEL_TO_EFFORT[reasoning_level]
            else:
                if reasoning_effort not in {"none", "low", "medium", "high"}:
                    reasoning_effort = "medium"
                reasoning_level = {
                    "none": "light",
                    "low": "light",
                    "medium": "intermediate",
                    "high": "high-high",
                }[reasoning_effort]
            generation_speed = self._bounded_text(
                payload.get("generation_speed"), 16
            ).casefold()
            speed_was_requested = generation_speed in self.GENERATION_SPEED_MULTIPLIER
            if not speed_was_requested:
                generation_speed = "medium"
            task_intensity = self._bounded_text(
                payload.get("task_intensity"), 16
            ).casefold()
            intensity_was_requested = task_intensity in self.TASK_INTENSITY_SETTINGS
            if not intensity_was_requested:
                task_intensity = "normal"
            intensity_settings = self.TASK_INTENSITY_SETTINGS[task_intensity]
            configured_token_budget = round(
                int(intensity_settings["base_output_tokens"])
                * self.GENERATION_SPEED_MULTIPLIER[generation_speed]
            )
            controls_were_requested = speed_was_requested or intensity_was_requested
            try:
                requested_max_tokens = int(
                    payload.get("max_output_tokens")
                    or (configured_token_budget if controls_were_requested else 768)
                )
            except (TypeError, ValueError):
                requested_max_tokens = (
                    configured_token_budget if controls_were_requested else 768
                )
            max_tokens = max(64, min(2_048, requested_max_tokens))
            if controls_were_requested:
                max_tokens = min(max_tokens, configured_token_budget)
            passthrough = {
                key: value
                for key, value in payload.items()
                if not str(key).startswith("_")
                and key
                not in {
                    "tool_id",
                    "request_id",
                    "message",
                    "history",
                    "prompt",
                    "max_output_tokens",
                    "runtime_model",
                    "programming_folder",
                    "reasoning_level",
                    "reasoning_effort",
                    "generation_speed",
                    "task_intensity",
                    "conversation_mode",
                    "previous_conversation_mode",
                    "context_budget_characters",
                    "local_hardware_profile",
                }
            }
            result = await self._request(
                "local_ai_infer",
                {
                    **passthrough,
                    "prompt": prompt,
                    "entry_mode": "user-command",
                    "user_command": raw_message,
                    "_command_context": command_context,
                    "max_output_tokens": max_tokens,
                    "runtime_model": runtime_model,
                    "programming_folder": self._bounded_text(
                        payload.get("programming_folder"), 1_024
                    ) if conversation_mode == "coding" else "",
                    "conversation_mode": conversation_mode,
                    "interaction_mode": f"model-dialogue-{conversation_mode}",
                    "context_budget_characters": self._context_budget(payload),
                    "reasoning_level": reasoning_level,
                    "reasoning_effort": reasoning_effort,
                    "generation_speed": generation_speed,
                    "task_intensity": task_intensity,
                    "task_intensity_mode": "automatic",
                    "requested_task_intensity": task_intensity,
                    "reasoning_path": intensity_settings["reasoning_path"],
                    "model_selection_strategy": intensity_settings[
                        "model_selection_strategy"
                    ],
                    "autonomous_agent": True,
                    "automatic_workflow": True,
                    "workflow_sequence": list(self.AUTOMATIC_WORKFLOW_SEQUENCE),
                    "primary_language": self.PRIMARY_LANGUAGE,
                    "_runtime_model_selection_authorized": bool(runtime_model),
                },
                timeout_seconds=600,
                parent_request_id=request_id,
                progress_callback=progress_callback,
                cancel_event=payload.get("_cancel_event"),
            )
            result["channel_workflow"] = {
                "automatic": True,
                "primary_language": self.PRIMARY_LANGUAGE,
                "sequence": list(self.AUTOMATIC_WORKFLOW_SEQUENCE),
            }
            return f"{command}_result", result
        raise PermissionError("PERMISSION_DENIED")


__all__ = ["StarChatService"]
