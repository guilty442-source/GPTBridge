from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from typing import Any, Mapping


class LocalAiCommandMixin:
    def _understand_command_with_qwen(
        self,
        command: str,
        *,
        context: str = "",
        confirmed: bool = False,
        programming_folder: str,
        understanding_model: str = "",
    ) -> dict[str, Any]:
        selected_understanding_model = (
            str(understanding_model).strip() or self.COMMAND_UNDERSTANDING_MODEL
        )
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "command": command,
                    "context": context,
                    "confirmed": confirmed,
                    "programming_folder": programming_folder,
                    "understanding_model": selected_understanding_model,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = time.time()
        with self._command_understanding_cache_lock:
            cached = self._command_understanding_cache.get(cache_key)
            if cached and now - cached[0] <= self.COMMAND_UNDERSTANDING_CACHE_TTL_SECONDS:
                result = copy.deepcopy(cached[1])
                result["cache_hit"] = True
                return result
        allowed_intents = sorted(self.transformer_runtime.INTENT_MODEL_PREFERENCES)
        understanding_prompt = (
            "你是所有任務的強制命令理解階段。分析原始繁體中文／台灣中文命令，"
            "不要執行任務。只輸出單一 JSON 物件，不要 Markdown。JSON 必須包含："
            "intents（字串陣列）、task_intensity（simple/normal/intermediate/difficult）、"
            "actions（字串陣列）、objects（字串陣列）、parameters（物件）、"
            "constraints（字串陣列）、confirmation_required（布林值）、"
            "remaining_ambiguities（字串陣列）、reason（字串）。"
            f"允許的 intents：{allowed_intents}。"
            f"是否已有明確確認：{confirmed}。"
            f"最近對話上下文：{context or '無'}\n"
            f"原始命令：{command}"
        )
        understanding_prompt += (
            "\n請直接理解原始繁體中文（台灣）語意，不先翻譯成其他語言。"
            "JSON 必須包含 original_command_zh_tw、intents、"
            "task_intensity、actions、objects、parameters、constraints、"
            "confirmation_required、remaining_ambiguities、reason。"
            f"本次使用者選擇的編程頂層資料夾是 {programming_folder}；所有相對路徑以此為基準。"
            "不得規劃、讀取、寫入或執行此頂層資料夾以外的目標；不得使用 ..、"
            "外部絕對路徑、捷徑或符號連結繞過範圍。"
        )
        inference = self.transformer_runtime.generate(
            prompt=understanding_prompt,
            intent="command_understanding",
            model_role="mandatory-command-understanding-for-all-tasks",
            output={"response": ""},
            max_tokens=384,
            reasoning_effort="low",
            task_intensity="normal",
            requested_model=selected_understanding_model,
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
            _automatic_model_override=True,
            response_format="json",
        )
        if inference.get("ok") is not True:
            return {"ok": False, "inference": inference}
        text = str(inference.get("text") or "").strip()
        decoded: Any = None
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if match is not None:
                try:
                    decoded = json.loads(match.group(0))
                except json.JSONDecodeError:
                    decoded = None
        if not isinstance(decoded, dict):
            repair = self.transformer_runtime.generate(
                prompt=(
                    "將下列內容修復成單一有效 JSON 物件。不得加入 Markdown、說明或程式碼圍欄；"
                    "保留可辨識資訊，缺少的欄位使用空陣列、空物件或 false。\n"
                    f"待修復內容：\n{text[:12_000]}"
                ),
                intent="command_understanding",
                model_role="command-understanding-json-repair",
                output={"response": ""},
                max_tokens=384,
                reasoning_effort="none",
                task_intensity="simple",
                requested_model=selected_understanding_model,
                complex_pipeline=False,
                reasoning_pipeline=False,
                division_pipeline=False,
                _automatic_model_override=True,
                response_format="json",
            )
            if repair.get("ok") is True:
                try:
                    decoded = json.loads(str(repair.get("text") or "").strip())
                except json.JSONDecodeError:
                    decoded = None
            if not isinstance(decoded, dict):
                return {
                    "ok": False,
                    "inference": repair if repair.get("ok") is not True else inference,
                    "error_code": "COMMAND_UNDERSTANDING_OUTPUT_INVALID",
                }
        if not isinstance(decoded, dict):
            return {
                "ok": False,
                "inference": inference,
                "error_code": "COMMAND_UNDERSTANDING_OUTPUT_INVALID",
            }
        intents = [
            str(item).strip().casefold()
            for item in decoded.get("intents") or []
            if str(item).strip().casefold()
            in self.transformer_runtime.INTENT_MODEL_PREFERENCES
        ]
        if not intents:
            intents = ["conversation"]
        intensity = str(decoded.get("task_intensity") or "normal").casefold()
        if intensity not in self.transformer_runtime.TASK_LEVEL_LABELS:
            intensity = "normal"
        plan = {
            "schema": "qwen-command-plan/v1",
            "original_command": command,
            "command_language": "zh-TW",
            "programming_scope": {
                "project_root": programming_folder,
                "selected_folder": programming_folder,
                "outside_project_access": False,
            },
            "context": context,
            "intents": list(dict.fromkeys(intents)),
            "primary_intent": intents[0],
            "actions": list(decoded.get("actions") or []),
            "objects": list(decoded.get("objects") or []),
            "parameters": dict(decoded.get("parameters") or {}),
            "constraints": list(decoded.get("constraints") or []),
            "task_intensity": {
                "level": intensity,
                "label": self.transformer_runtime.TASK_LEVEL_LABELS[intensity],
                "reason": str(decoded.get("reason") or ""),
            },
            "command_understanding": {
                "recognized": True,
                "model": selected_understanding_model,
                "star_native_model_used": False,
            },
            "safety": {
                "confirmation_required": bool(
                    decoded.get("confirmation_required") is True and not confirmed
                ),
                "remaining_ambiguities": list(
                    decoded.get("remaining_ambiguities") or []
                ),
            },
        }
        result = {"ok": True, "plan": plan, "inference": inference, "cache_hit": False}
        with self._command_understanding_cache_lock:
            self._command_understanding_cache[cache_key] = (
                now,
                copy.deepcopy(result),
            )
            while (
                len(self._command_understanding_cache)
                > self.COMMAND_UNDERSTANDING_CACHE_MAX_ENTRIES
            ):
                oldest = min(
                    self._command_understanding_cache,
                    key=lambda key: self._command_understanding_cache[key][0],
                )
                self._command_understanding_cache.pop(oldest, None)
        return result

    def _run_rnj_frontend_worker(
        self,
        command: str,
        command_plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.transformer_runtime.generate(
            prompt=(
                "命令理解已由 Qwen3.8 完成。你位於流程前端，只做 Code／STEM、"
                "數學與 Tool Calling 結構解析，不得改寫已判定的命令意圖，也不得執行工具。\n"
                f"原始命令：{command}\n"
                "Qwen3.8 命令理解："
                f"{json.dumps(dict(command_plan), ensure_ascii=False, separators=(',', ':'))}"
            ),
            intent="command_understanding",
            model_role="mandatory-rnj-frontend-after-command-understanding",
            output={"response": ""},
            max_tokens=192,
            reasoning_effort="low",
            task_intensity=str(
                (command_plan.get("task_intensity") or {}).get("level") or "normal"
            ),
            requested_model=self.transformer_runtime.FRONTEND_WORKER_MODEL,
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
        )

    def _ollama_model_for_intent(
        self, intent: str, task_intensity: str = ""
    ) -> str:
        normalized_intent = str(intent or "").strip()
        normalized_intensity = str(task_intensity or "").strip().casefold()
        if (
            normalized_intent in {"coding", "command_execution"}
            and normalized_intensity in {"simple", "normal"}
        ):
            return "granite-code:3b"
        role_routes = {
            "conversation": "glm4:9b",
            "reading": "gemma4:12b-it-qat",
            "search": "mistral-small:24b",
            "data": "ibm/granite4.2:30b-q4_K_M",
            "data_organization": "ibm/granite4.2:30b-q4_K_M",
            "capabilities": "gemma4:26b-a4b-it-qat",
            "calculation": self.MATHEMATICAL_REVIEW_MODEL,
            "statistics": self.MATHEMATICAL_REVIEW_MODEL,
            "reasoning": self.MATHEMATICAL_REVIEW_MODEL,
            "analysis": self.MATHEMATICAL_REVIEW_MODEL,
            "risk": self.MATHEMATICAL_REVIEW_MODEL,
            "coding": self.CODING_EXPERT_MODEL,
            "self_upgrade": self.CODING_EXPERT_MODEL,
            "command_understanding": self.COMMAND_UNDERSTANDING_MODEL,
            "command_execution": self.CODING_EXPERT_MODEL,
            "autonomous_agent": "nemotron-3.5-lightning:30b-a3b-q4_K_M",
            "visual": self.transformer_runtime.VISUAL_FILE_MANAGEMENT_MODEL,
            "fast_visual": "gemma4:e2b-it-qat",
            "multimodal": "gemma4:12b-it-qat",
            "advanced_multimodal": "gemma4:26b-a4b-it-qat",
            "visual_reasoning": "qwen3-vl:8b-thinking",
            "visual_rag": "qwen3-vl:8b-thinking",
            "file_management": self.CODING_EXPERT_MODEL,
            "training": self.TRAINING_COORDINATOR_MODEL,
        }
        return role_routes.get(normalized_intent, self.DATA_COORDINATOR_MODEL)

    def _arrange_ollama_tasks(self, intents: list[str]) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(str(item).strip() for item in intents if item))
        if not normalized:
            normalized = ["conversation"]
        return [
            {
                "sequence": index,
                "intent": intent,
                "assigned_model": self._ollama_model_for_intent(intent),
                "selection": "fixed-primary-owner-no-backup",
                "project_scope": "all-project-source-excluding-governance-rule",
                "star_native_model_included": False,
                "external_ai_used": False,
            }
            for index, intent in enumerate(normalized, start=1)
        ]
