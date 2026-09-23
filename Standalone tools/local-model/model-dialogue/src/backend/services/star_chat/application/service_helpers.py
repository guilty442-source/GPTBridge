from __future__ import annotations

from typing import Any


class StarChatHelpersMixin:
    """Prompt-building and generation-control helpers for StarChatService."""

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
    CHAT_INSTRUCTION = (
        "以自然、友善、直接的對話方式回應；問答、討論、解釋、內容協作與程式"
        "任務皆在對話內完成。使用者設定程式作業資料夾時，以受治理的唯讀工作"
        "區工具脈絡處理程式需求，優先提供可執行、可驗證的方案。"
    )

    @staticmethod
    def _bounded_text(value: Any, maximum: int) -> str:
        return str(value or "").strip()[:maximum]

    @classmethod
    def _conversation_prompt(cls, payload: dict[str, Any]) -> str:
        message = cls._bounded_text(payload.get("message") or payload.get("prompt"), 32_000)
        instruction = cls.CHAT_INSTRUCTION
        persona = cls._bounded_text(payload.get("persona"), 4_000)
        persona_block = (
            f"星澄人格設定：\n{persona}\n\n" if persona else ""
        )
        context = cls._conversation_context(payload)
        if not context:
            return f"{persona_block}{instruction}\n\n使用者最新訊息：{message}"
        return f"{persona_block}{instruction}\n\n以下是同一段對話的最近內容：\n{context}\n\n使用者最新訊息：{message}"

    @staticmethod
    def _context_budget(payload: dict[str, Any]) -> int:
        try:
            requested = int(payload.get("context_budget_characters") or 500_000)
        except (TypeError, ValueError):
            requested = 500_000
        return max(4_000, min(600_000, requested))

    @classmethod
    def _conversation_context(cls, payload: dict[str, Any]) -> str:
        history = payload.get("history")
        context_budget = cls._context_budget(payload)
        rows: list[str] = []
        if isinstance(history, list):
            for item in history[-50:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").strip().casefold()
                if role not in {"user", "assistant"}:
                    continue
                content = cls._bounded_text(item.get("content"), 32_000)
                if content:
                    rows.append(f"{'使用者' if role == 'user' else '星澄'}：{content}")
        return "\n".join(rows)[-context_budget:]

    @classmethod
    def _channel_workflow(cls) -> dict[str, Any]:
        return {
            "automatic": True,
            "primary_language": cls.PRIMARY_LANGUAGE,
            "sequence": list(cls.AUTOMATIC_WORKFLOW_SEQUENCE),
        }

    @classmethod
    def _resolve_reasoning(cls, payload: dict[str, Any]) -> tuple[str, str]:
        reasoning_level = cls._bounded_text(
            payload.get("reasoning_level"), 32
        ).casefold()
        reasoning_effort = cls._bounded_text(
            payload.get("reasoning_effort"), 16
        ).casefold()
        if reasoning_level in cls.REASONING_LEVEL_TO_EFFORT:
            return reasoning_level, cls.REASONING_LEVEL_TO_EFFORT[reasoning_level]
        if reasoning_effort not in {"none", "low", "medium", "high"}:
            reasoning_effort = "medium"
        reasoning_level = {
            "none": "light",
            "low": "light",
            "medium": "intermediate",
            "high": "high-high",
        }[reasoning_effort]
        return reasoning_level, reasoning_effort

    @classmethod
    def _resolve_generation_controls(
        cls, payload: dict[str, Any]
    ) -> dict[str, Any]:
        reasoning_level, reasoning_effort = cls._resolve_reasoning(payload)
        raw_reasoning_level = cls._bounded_text(
            payload.get("reasoning_level"), 32
        ).casefold()
        raw_reasoning_effort = cls._bounded_text(
            payload.get("reasoning_effort"), 16
        ).casefold()
        reasoning_was_requested = (
            raw_reasoning_level in cls.REASONING_LEVEL_TO_EFFORT
            or raw_reasoning_effort in {"none", "low", "medium", "high"}
        )
        generation_speed = cls._bounded_text(
            payload.get("generation_speed"), 16
        ).casefold()
        speed_was_requested = generation_speed in cls.GENERATION_SPEED_MULTIPLIER
        if not speed_was_requested:
            generation_speed = "medium"
        task_intensity = cls._bounded_text(
            payload.get("task_intensity"), 16
        ).casefold()
        intensity_was_requested = task_intensity in cls.TASK_INTENSITY_SETTINGS
        if not intensity_was_requested:
            task_intensity = "normal"
        intensity_settings = cls.TASK_INTENSITY_SETTINGS[task_intensity]
        configured_token_budget = round(
            int(intensity_settings["base_output_tokens"])
            * cls.GENERATION_SPEED_MULTIPLIER[generation_speed]
        )
        max_tokens = cls._resolve_max_tokens(
            payload,
            configured_token_budget,
            speed_was_requested or intensity_was_requested,
        )
        return {
            "runtime_model": cls._bounded_text(payload.get("runtime_model"), 256),
            "reasoning_level": reasoning_level,
            "reasoning_effort": reasoning_effort,
            "reasoning_was_requested": reasoning_was_requested,
            "generation_speed": generation_speed,
            "speed_was_requested": speed_was_requested,
            "task_intensity": task_intensity,
            "intensity_was_requested": intensity_was_requested,
            "intensity_settings": intensity_settings,
            "max_tokens": max_tokens,
        }

    @staticmethod
    def _resolve_max_tokens(
        payload: dict[str, Any],
        configured_token_budget: int,
        controls_were_requested: bool,
    ) -> int:
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
        return max_tokens

    _PASSTHROUGH_EXCLUDE = frozenset(
        {
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
            "persona",
        }
    )

    @classmethod
    def _infer_passthrough(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if not str(key).startswith("_")
            and key not in cls._PASSTHROUGH_EXCLUDE
        }

    def _infer_payload(
        self,
        payload: dict[str, Any],
        prompt: str,
        raw_message: str,
        command_context: str,
        controls: dict[str, Any],
    ) -> dict[str, Any]:
        intensity_settings = controls["intensity_settings"]
        inference_payload = {
            **self._infer_passthrough(payload),
            "prompt": prompt,
            "entry_mode": "user-command",
            "user_command": raw_message,
            "_command_context": command_context,
            "max_output_tokens": controls["max_tokens"],
            "runtime_model": controls["runtime_model"],
            "programming_folder": self._bounded_text(
                payload.get("programming_folder"), 1_024
            ),
            "conversation_mode": "chat",
            "interaction_mode": "model-dialogue-chat",
            "persona": self._bounded_text(payload.get("persona"), 4_000),
            "context_budget_characters": self._context_budget(payload),
            "task_intensity_mode": "automatic",
            "reasoning_path": intensity_settings["reasoning_path"],
            "model_selection_strategy": intensity_settings[
                "model_selection_strategy"
            ],
            "autonomous_agent": True,
            # P21：model-dialogue 是工具迴圈（converse）的指定生產面——
            # 預設啟用讓模型可發唯讀工具呼叫與系統修改提案；
            # 呼叫端可顯式傳 tools_enabled=False 退回扁平生成。
            "tools_enabled": payload.get("tools_enabled") is not False,
            "automatic_workflow": True,
            "workflow_sequence": list(self.AUTOMATIC_WORKFLOW_SEQUENCE),
            "primary_language": self.PRIMARY_LANGUAGE,
            "_runtime_model_selection_authorized": bool(controls["runtime_model"]),
        }
        # Only explicit user controls are forwarded: a default must never
        # masquerade as a request and override the model service's own
        # traditional-Chinese command assessment (it correctly rates a plain
        # greeting as simple, which keeps the fast conversation path).
        if controls["reasoning_was_requested"]:
            inference_payload["reasoning_level"] = controls["reasoning_level"]
            inference_payload["reasoning_effort"] = controls["reasoning_effort"]
        if controls["speed_was_requested"]:
            inference_payload["generation_speed"] = controls["generation_speed"]
        if controls["intensity_was_requested"]:
            inference_payload["task_intensity"] = controls["task_intensity"]
            inference_payload["requested_task_intensity"] = controls["task_intensity"]
        return inference_payload


__all__ = ["StarChatHelpersMixin"]
