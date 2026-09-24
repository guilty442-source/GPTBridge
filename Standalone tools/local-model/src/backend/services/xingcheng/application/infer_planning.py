from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from .investment_analysis import analyze_investments


def _tool_loop_system_prompt() -> str:
    """P21 工具迴圈的預設 system prompt：宣告 star-tool-call/v1 白名單。"""
    import json

    from .native_tool_orchestrator import GovernedToolExecutor

    specs = json.dumps(
        GovernedToolExecutor.available_tool_specs(), ensure_ascii=False
    )
    return (
        "你是星澄，一個本地模型。當需要查詢系統狀態或提出系統修改時，"
        '只輸出 <tool_call>{"name": "<工具名>", "arguments": {...}}</tool_call>'
        f"，不要輸出其他內容。可用工具（star-tool-call/v1）：{specs}"
    )


class InferPlanningMixin:

    async def _infer_prepare_intents(
        self,
        payload: dict[str, Any],
        command_plan: dict[str, Any],
        raw_command: str,
    ) -> tuple[dict[str, Any], str, list[str], str, list[dict[str, Any]]]:
        inference_payload = dict(payload)
        inference_payload["_semantic_plan"] = command_plan
        assessed_intensity = str(
            (command_plan.get("task_intensity") or {}).get("level") or "normal"
        ).strip().casefold()
        requested_intensity = str(
            inference_payload.get("requested_task_intensity")
            or inference_payload.get("task_intensity")
            or ""
        ).strip().casefold()
        automatic_intensity = (
            requested_intensity
            if requested_intensity in {"simple", "normal", "intermediate", "difficult"}
            else assessed_intensity
        )
        if automatic_intensity not in {
            "simple",
            "normal",
            "intermediate",
            "difficult",
        }:
            automatic_intensity = "normal"
        if payload.get("_task_intensity_override_authorized") is not True:
            inference_payload["task_intensity"] = automatic_intensity
            inference_payload["task_intensity_source"] = (
                "automatic-traditional-chinese-command-assessment"
            )
            inference_payload["max_output_tokens"] = {
                "simple": 256,
                "normal": 512,
                "intermediate": 768,
                "difficult": 1_024,
            }[automatic_intensity]
        self._runtime_metrics["inference_request_count"] = int(
            self._runtime_metrics["inference_request_count"]
        ) + 1
        self._runtime_metrics["last_activity_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        prompt = str(
            inference_payload.get("instruction")
            or inference_payload.get("prompt")
            or ""
        )
        planned_intents = list(command_plan.get("intents") or [])
        if not planned_intents:
            planned_intents = ["conversation"]
        if self.reading_expert.has_readable_content(inference_payload):
            planned_intents = [
                "reading",
                *(intent for intent in planned_intents if intent != "reading"),
            ]
        external_tasks: list[dict[str, Any]] = []
        planned_intent = planned_intents[0]
        await asyncio.to_thread(
            self._repository_for(self.models.MAIN).record_common_command,
            raw_command,
            planned_intent,
        )
        return (
            inference_payload,
            prompt,
            planned_intents,
            planned_intent,
            external_tasks,
        )

    async def _infer_validate_request(
        self,
        payload: dict[str, Any],
    ) -> tuple[tuple[str, dict[str, Any]] | None, str, bool, str]:
        capability_request = payload.get("capability_composition")
        if isinstance(capability_request, dict):
            if (
                str(payload.get("runtime_model") or "") != self.NATIVE_MODEL_ID
                or payload.get("_native_internal_operation") is not True
            ):
                return ("xingcheng_infer_result", {
                    "ok": False,
                    "error_code": "NATIVE_INTERNAL_OPERATION_REQUIRED",
                    "message": "能力編成只由星澄原生模型內部處理。",
                }), "", False, ""
            result = await self._compose_capability_with_vote(capability_request)
            result["internal_owner"] = self.NATIVE_MODEL_ID
            return ("xingcheng_infer_result", result), "", False, ""
        manual_model_fields = (
            "model",
            "model_id",
            "assigned_model",
            "specialist",
        )
        if any(str(payload.get(key) or "").strip() for key in manual_model_fields):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "DIRECT_MODEL_ACCESS_DENIED",
                "message": "模型由星澄主模型自動安排，不允許直接指定。",
                "model_selection": "automatic",
            }), "", False, ""
        requested_runtime_model = str(payload.get("runtime_model") or "").strip()
        native_model_requested = requested_runtime_model == self.NATIVE_MODEL_ID
        direct_runtime_model = (
            "" if native_model_requested else requested_runtime_model
        )
        if (
            requested_runtime_model
            and payload.get("_runtime_model_selection_authorized") is not True
        ):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "RUNTIME_MODEL_SELECTION_DENIED",
                "message": "只有受治理的模型對話工具可以選擇本機生成模型。",
            }), "", False, ""
        if requested_runtime_model and not native_model_requested:
            selectable_names = {
                str(item.get("name") or "")
                for item in self.native_runtime.selectable_models(refresh=False)
            }
            if requested_runtime_model not in selectable_names:
                return ("xingcheng_infer_result", {
                    "ok": False,
                    "error_code": "RUNTIME_MODEL_NOT_INSTALLED",
                    "message": "選擇的模型未安裝或不符合本機模型名稱規則。",
                    "selectable_models": sorted(selectable_names),
                }), "", False, ""
        if (
            requested_runtime_model
            and not native_model_requested
            and not self.native_runtime.enabled
        ):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "LOCAL_MODEL_RUNTIME_REQUIRED",
                "message": "本地模型執行環境未啟用；任務不會回退交給星澄。",
                "star_native_model_used": False,
                "fallback_model_used": False,
            }), "", False, ""
        return None, requested_runtime_model, native_model_requested, direct_runtime_model

    def _infer_build_command_plan(
        self,
        payload: dict[str, Any],
    ) -> tuple[
        tuple[str, dict[str, Any]] | None,
        str,
        str,
        dict[str, Any],
        Any,
        str,
    ]:
        prompt = str(payload.get("instruction") or payload.get("prompt") or "")
        raw_command = str(payload.get("user_command") or prompt).strip()
        requested_programming_folder = self._programming_folder_for_request(payload)
        if not requested_programming_folder:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_REQUIRED",
                "message": "請先選擇編程資料夾。",
            }), "", "", {}, None, ""
        try:
            programming_folder_path = Path(requested_programming_folder).resolve()
        except OSError:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_INVALID",
                "message": "選擇的編程資料夾路徑無效。",
            }), "", "", {}, None, ""
        if not programming_folder_path.is_dir():
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_NOT_FOUND",
                "message": "選擇的編程資料夾不存在或不是資料夾。",
            }), "", "", {}, None, ""
        programming_folder = str(programming_folder_path)
        payload["programming_folder"] = programming_folder
        command_context = str(payload.get("_command_context") or "").strip()
        textual_confirmation = bool(
            re.match(
                r"^\s*(?:我(?:已)?|本人)?\s*確認(?:執行|刪除|操作|繼續)",
                raw_command,
                flags=re.IGNORECASE,
            )
        )
        explicit_confirmation = bool(
            payload.get("confirmed") is True
            or payload.get("destructive_confirmation") is True
            or textual_confirmation
        )
        command_understanding_model = "deterministic-zh-tw-fast-path"
        progress_callback = payload.get("_progress_callback")
        if callable(progress_callback):
            progress_callback({
                "phase": "command-understanding",
                "model": command_understanding_model,
                "message": "正在直接理解繁體中文並建立任務計畫",
            })
        command_plan = self._repository_for(self.models.MAIN).command_parser.parse(
            raw_command,
            self.native_model.semantic_plan(
                raw_command,
                context=command_context,
                confirmed=explicit_confirmation,
            ),
        )
        command_plan["programming_scope"] = {
            "project_root": programming_folder,
            "selected_folder": programming_folder,
            "outside_project_access": False,
        }
        command_plan["command_understanding"] = {
            "recognized": True,
            "model": "deterministic-zh-tw-fast-path",
            "star_native_model_used": False,
        }
        return None, prompt, raw_command, command_plan, progress_callback, command_understanding_model

    async def _infer_run_command_preflight_gates(
        self,
        payload: dict[str, Any],
        command_plan: dict[str, Any],
        raw_command: str,
        prompt: str,
        native_model_requested: bool,
        progress_callback: Any,
        direct_runtime_model: str,
    ) -> tuple[str, dict[str, Any]] | None:
        run_frontend_worker = (
            not direct_runtime_model
            and not native_model_requested
            and str(payload.get("task_intensity") or "normal").strip().casefold()
            == "difficult"
            and
            str(payload.get("generation_speed") or "medium").strip().casefold()
            not in {"high", "ultra"}
            and (
                str(payload.get("conversation_mode") or "").strip().casefold() == "coding"
                or any(
                    intent in {"coding", "calculation", "statistics", "reasoning", "command_execution"}
                    for intent in command_plan.get("intents") or []
                )
            )
        )
        if callable(progress_callback) and run_frontend_worker:
            progress_callback({
                "phase": "workflow-frontend",
                "model": self.native_runtime.FRONTEND_WORKER_MODEL,
                "message": "正在整理執行流程與模型路由",
            })
        if run_frontend_worker:
            frontend_result = await asyncio.to_thread(
                self._run_native_frontend_worker,
                raw_command,
                command_plan,
            )
            command_plan["frontend_worker"] = {
                "ok": frontend_result.get("ok") is True,
                "model": str(
                    frontend_result.get("model")
                    or self.native_runtime.FRONTEND_WORKER_MODEL
                ),
                "text": str(frontend_result.get("text") or "")[:8_000],
                "position": "after-command-understanding-for-code-and-stem",
                "optional": True,
                "dynamic_reassignment": dict(
                    frontend_result.get("dynamic_model_reassignment") or {}
                ),
            }
        safety = command_plan.get("safety")
        if isinstance(safety, dict) and safety.get("confirmation_required") is True:
            return "xingcheng_infer_result", {
                "ok": False,
                "error_code": "SAFE_CONFIRMATION_REQUIRED",
                "message": (
                    "命令涉及可能的破壞性操作，且利用目前上下文補全後仍有歧義。"
                    "請明確指出操作對象與範圍，並確認是否執行；目前未執行任何操作。"
                ),
                "status": "confirmation-required",
                "operation_executed": False,
                "semantic_understanding": command_plan,
                "remaining_ambiguities": list(
                    safety.get("remaining_ambiguities") or []
                ),
                "safety_gate": safety,
            }
        if native_model_requested and any(
            token in prompt.casefold()
            for token in (
                "用原生模型訓練星澄",
                "原生模型訓練星澄",
                "本地模型訓練星澄",
                "本機模型訓練星澄",
                "星澄自我訓練",
                "train star natively",
            )
        ):
            result = await self._train_with_native(payload)
            result["intent"] = "native_model_training"
            self._identify_model(result, self.models.primary)
            return "xingcheng_infer_result", result
        if (
            not native_model_requested
            and "參數" in prompt
            and any(token in prompt for token in ("調整", "修改", "優化", "校準"))
        ):
            result = await self._tune_investment_parameters(payload)
            result["intent"] = "investment_parameter_tuning"
            self._identify_model(result, self.models.primary)
            return "xingcheng_infer_result", result
        return None

    def _infer_plan_session_context(
        self,
        inference_payload: dict[str, Any],
        planned_intent: str,
        prompt: str,
        direct_runtime_model: str,
        native_model_requested: bool,
        command: str,
    ) -> tuple[
        tuple[str, dict[str, Any]] | None,
        str,
        Any,
        str,
        str,
    ]:
        automatic_runtime_model = (
            ""
            if direct_runtime_model
            else self.native_runtime.select_model_for_request(
                planned_intent,
                reasoning_effort=str(inference_payload.get("reasoning_effort") or "medium"),
                task_intensity=str(inference_payload.get("task_intensity") or "normal"),
                generation_speed=str(inference_payload.get("generation_speed") or "medium"),
                tier_cap=(
                    # Interactive dialogue must never pay a reload
                    # cycle — route every intent through the resident
                    # native model unless the user explicitly chose a
                    # harder intensity or a concrete model.
                    "resident"
                    if str(inference_payload.get("interaction_mode") or "")
                    .strip()
                    .casefold()
                    .startswith("model-dialogue")
                    and str(inference_payload.get("task_intensity") or "")
                    .strip()
                    .casefold()
                    != "difficult"
                    else ""
                ),
            )
        )
        if (
            planned_intent
            in self.native_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            and not self.native_runtime.enabled
        ):
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "VISUAL_SPECIALIST_UNAVAILABLE",
                "message": (
                    "原生模型為純文字架構，不支援視覺輸入；"
                    "未改派其他模型，也未猜測視覺內容。"
                ),
                "intent": planned_intent,
                "required_model": (
                    self.native_runtime.VISUAL_FILE_MANAGEMENT_MODEL
                ),
            }), "", "", "", ""
        if planned_intent in self.native_runtime.VISUAL_FILE_MANAGEMENT_INTENTS:
            return ("xingcheng_infer_result", {
                "ok": False,
                "error_code": "VISUAL_INPUT_UNSUPPORTED",
                "message": (
                    "原生模型為純文字架構，不支援圖片或影片輸入；"
                    "未改派其他模型，也未猜測視覺內容。"
                ),
                "intent": planned_intent,
                "required_model": self.native_runtime.VISUAL_FILE_MANAGEMENT_MODEL,
            }), "", "", "", ""
        inference_payload["_governed_intent"] = planned_intent
        planned_profile = (
            self.models.MAIN
            if native_model_requested
            else self.models.for_command(command, intent=planned_intent)
        )
        business_scope = (
            "investment"
            if not native_model_requested
            and (
                planned_profile == self.models.INVESTMENT
                or planned_intent in {"search", "analysis", "risk"}
            )
            else "general"
        )
        inference_payload["memory_context"] = (
            self.memory_broker.context_for_inference(
                business_scope,
                planned_intent,
                prompt,
                owner_only=True,
            )
            if native_model_requested
            else []
        )
        if native_model_requested:
            inference_payload["native_private_context"] = self._repository_for(
                self.models.MAIN
            ).native_private_context()
        # Fault-diagnosis grounding: when the user asks about a fault,
        # attach the governed evidence pack (fault-code directory matches,
        # maintenance manuals, bounded runtime state, outbox tail) so the
        # answering model works from authoritative data instead of guessing.
        # Read-only; repair remains owned by main-system central repair.
        if self.fault_diagnostics.looks_like_fault(prompt):
            inference_payload["fault_diagnostics"] = (
                self.fault_diagnostics.diagnose(prompt)
            )
        # Local-RAG grounding: bounded read-only retrieval from the
        # governed corpus (canonical-first while warm, bounded local
        # mirror otherwise). Skipped when the corpus is empty; failure
        # never blocks inference.
        rag_context = self._infer_rag_context(prompt)
        if rag_context:
            inference_payload["rag_context"] = rag_context
        if planned_intent in {"analysis", "risk"} and not native_model_requested:
            governed_parameters = self.investment_repository.investment_parameter_values()
            requested_parameters = inference_payload.get("analysis_parameters")
            if not isinstance(requested_parameters, dict):
                requested_parameters = {}
            inference_payload["analysis_parameters"] = {
                "position_concentration_percent": governed_parameters[
                    "max_single_position_percent"
                ],
                "missing_data_warning_percent": governed_parameters[
                    "missing_data_warning_percent"
                ],
                **requested_parameters,
            }
        if planned_profile.network_policy == "disabled":
            inference_payload["allow_network"] = False
        assigned_model = (
            self.NATIVE_MODEL_ID
            if native_model_requested
            else direct_runtime_model
            or automatic_runtime_model
            or self._runtime_model_for_intent(
                planned_intent,
                str(inference_payload.get("task_intensity") or "normal"),
            )
        )
        progress_callback = inference_payload.get("_progress_callback")
        if callable(progress_callback):
            progress_callback(
                {
                    "phase": "command-planned",
                    "model": assigned_model,
                    "intent": planned_intent,
                }
            )
        return None, automatic_runtime_model, planned_profile, business_scope, assigned_model

    async def _infer_converse_tools(
        self,
        inference_payload: dict[str, Any],
        prompt: str,
        planned_intent: str,
    ) -> dict[str, Any] | None:
        """P21：``tools_enabled`` 時改走 ChatSession + GovernedToolExecutor 迴圈。

        工具迴圈只在 Python 原生引擎上運行（C++ runtime 無 session 支援）；
        ``XINGCHENG_CPP_RUNTIME=required`` 或引擎不可用時回 ``None``，
        讓呼叫端退回一般生成路徑（提案仍由 finalize 文本掃描浮上）。
        模型本身不執行工具——全部呼叫經 ``GovernedToolExecutor`` 走
        ``self.handle`` 受管分派，``system_modification_proposals`` 僅
        浮上結果、由 model-dialogue 側轉交 ``app:propose-system-modification``。
        """
        try:
            from ..infrastructure.native_transformer.cpp_runtime import (
                cpp_runtime_mode,
            )

            if cpp_runtime_mode() == "required":
                return None
            from ..infrastructure.native_engine import (
                cpu_generation_cap,
                generation_defaults,
                native_engine_for,
            )
            from .native_tool_orchestrator import GovernedToolExecutor
        except Exception:
            return None
        try:
            engine = native_engine_for()
            session = engine.new_chat_session(
                system_prompt=str(inference_payload.get("system_prompt") or "")
                or _tool_loop_system_prompt(),
            )
        except Exception:
            return None
        defaults = generation_defaults()
        max_new = int(defaults["max_new_tokens"])
        if getattr(getattr(engine, "device", None), "type", "") == "cpu":
            max_new = min(max_new, cpu_generation_cap())
        executor = GovernedToolExecutor(self)
        outcome = await executor.converse(
            session, str(prompt or ""), max_new_tokens=max_new
        )
        return {
            "ok": True,
            "model": "xingcheng-native-transformer",
            "model_family": "xingcheng-native",
            "architecture": "xingcheng-native-decoder-transformer",
            "mode": "governed-native-transformer-toolloop",
            "response": outcome["text"],
            "text": outcome["text"],
            "intent": planned_intent,
            "semantic_understanding": {"comprehension": {"command": {}}},
            "generation": {
                "text": outcome["text"],
                "model": "xingcheng-native-transformer",
                "model_type": "native-transformer-autoregressive-decoder",
                "token_count": int(outcome.get("generated_tokens") or 0),
            },
            "tool_trace": outcome.get("tool_trace") or [],
            "tool_rounds": int(outcome.get("tool_rounds") or 0),
            "rounds_exhausted": bool(outcome.get("rounds_exhausted")),
            "tool_calls_pending": outcome.get("tool_calls_pending") or [],
            "system_modification_proposals": list(
                outcome.get("system_modification_proposals") or []
            ),
            "tools_enabled": True,
            "native_engine": True,
            "star_native_model_used": True,
            "checkpoint_path": str(getattr(engine, "checkpoint_path", "") or ""),
            "state_sha256": str(getattr(engine, "state_sha256", "") or ""),
            "context_window": int(
                getattr(getattr(engine, "config", None),
                        "max_position_embeddings", 0) or 0
            ),
        }

    async def _infer_generate_model_output(
        self,
        inference_payload: dict[str, Any],
        prompt: str,
        planned_intent: str,
        planned_profile: Any,
        native_model_requested: bool,
    ) -> tuple[tuple[str, dict[str, Any]] | None, dict[str, Any]]:
        inference_started = time.perf_counter()
        # P21：tools_enabled 且本輪本就會走原生引擎（明確請求原生模型，
        # 或 transformer runtime 停用時的原生路徑）→ converse 工具迴圈。
        if bool(inference_payload.get("tools_enabled")) and (
            native_model_requested or not self.native_runtime.enabled
        ):
            tool_output = await self._infer_converse_tools(
                inference_payload, prompt, planned_intent
            )
            if tool_output is not None:
                self._record_latency("inference", inference_started)
                return None, tool_output
        output = (
            await asyncio.to_thread(
                self.model_engines.for_profile(planned_profile).infer,
                inference_payload,
                database=self._repository_for(planned_profile).database_status(),
                analyze=analyze_investments,
                search=self.market_data.search,
            )
            if native_model_requested or not self.native_runtime.enabled
            else await asyncio.to_thread(
                self._prepare_runtime_output,
                inference_payload,
                prompt,
                planned_intent,
            )
        )
        self._record_latency("inference", inference_started)
        if not output.get("ok"):
            self._runtime_metrics["error_count"] = int(
                self._runtime_metrics["error_count"]
            ) + 1
            return ("xingcheng_infer_result", output), None
        return None, output
