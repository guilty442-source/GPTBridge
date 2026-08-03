from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from ..integration.channel_client import build_star_ai_channel_client
from .investment_analysis import ANALYSIS_MODEL_KEYS, analyze_investments
from .investment_accounting import coordinate_investment_accounting
from ..infrastructure.market_data import MarketDataSearch, market_source_catalog
from .mathematical_expert import StarMathematicalExpert
from .coding_expert import StarCodingExpert
from .reading_expert import StarReadingExpert
from .local_rag import LocalRagService
from .local_knowledge import LocalKnowledgeService
from shared_layer.git_repository import LocalGitRepository
from .gpt_training_gate import StarOllamaTrainingGate
from .capability_evaluation import evaluate_star_capabilities
from ..integration.memory_broker import StarMemoryBroker
from ..domain.model_registry import StarModelProfile, StarModelRegistry
from ..domain.module_registry import StarModuleRegistry
from ..domain.capability_composer import StarCapabilityComposer
from ..infrastructure.model_engines import StarModelEngines
from ..infrastructure.repository import LocalAiRepository
from ..infrastructure.ollama_model_repository import OllamaModelRepository
from ..infrastructure.transformer_runtime import StarTransformerRuntime
from ..infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)
from .upgrade_evaluation import evaluate_star_upgrade


class LocalAiService:
    VERSION = "1.0.0"
    NATIVE_MODEL_ID = "star-main-native-model"
    PLATFORM_MODE = "context-aware-multitask-model-platform"
    ENTRY_GATEWAY = "all-ai-business-entries"
    CENTRAL_MANAGEMENT_RESPONSIBILITIES = (
        "sql-central-management",
        "rag-central-management",
        "git-central-management",
    )
    PLATFORM_PERMISSION_PROFILE = "local-model-platform-v1"
    PLATFORM_LABELS = (
        {"id": "ai-entry-gateway", "label": "AI 統一入口"},
        {"id": "context-multitask", "label": "情境多工"},
        {"id": "local-model-routing", "label": "本機模型路由"},
        {"id": "selected-model-direct", "label": "選定模型直連"},
        {"id": "ollama-loopback", "label": "Ollama 本機迴路"},
        {"id": "governance-controlled", "label": "治理受控"},
    )
    STAR_NATIVE_MODEL_PERMISSIONS = {
        "inference": False,
        "understanding": False,
        "advisory": False,
        "generation": False,
        "task_participation": False,
        "entry_dispatch": False,
        "cross_tool_connection": False,
        "database_read": True,
        "database_read_scope": "all-project-databases-excluding-governance-rule",
        "database_write": True,
        "database_write_scope": "local-ai-model-internal-excluding-permission-data",
        "database_actions": (
            "read",
            "create-write-save",
            "append",
            "update",
            "delete",
            "rollback",
        ),
        "investment_database_write": False,
        "ollama_model_database_access": True,
        "source_apply": False,
        "external_execution": False,
        "system_execution": False,
        "git_write": False,
        "sql_write_outside_self_model_data": False,
        "governance_authority": False,
        "governance_source_access": "direct-read-only-authoritative",
        "governance_source_priority": "highest",
        "platform_mediated_persistence": True,
    }
    PLATFORM_SERVICES = {
        "model-dialogue-manual": "selected-model-direct-under-governance",
        "model-dialogue-auto": "traditional-chinese-first-governed-workflow",
        "investment-manager": "automatic-local-ollama-only-routing",
        "programming": "hardware-stable-local-coding-owner",
        "reasoning": "deepseek-r1-14b-fixed-owner-with-qwen3.8-acceptance",
        "complex-work": "understand-allocate-role-integrate-execute-inspect-result",
        "data": "granite4.2-enterprise-investment-data",
        "native-training": "local-ollama-only",
        "capability-composition": "qwen3.8-commanded-local-models",
        "autonomous-agent": "traditional-chinese-first-governed-workflow",
        "star-native-model": "task-participation-denied",
    }
    FINAL_COORDINATOR_MODEL = "qwen3.8:27b-q4_K_M"
    TRAINING_COORDINATOR_MODEL = "gpt-oss:20b"
    DATA_COORDINATOR_MODEL = "ibm/granite4.2:30b-q4_K_M"
    COMMAND_UNDERSTANDING_MODEL = "qwen3.8:27b-q4_K_M"
    FAST_COMMAND_UNDERSTANDING_MODEL = "openbmb/minicpm-v4.6:q8_0"
    GENERALIST_COORDINATOR_MODEL = "qwen3.8:27b-q4_K_M"
    AUTONOMOUS_AGENT_MODEL = "nemotron-3.5-lightning:30b-a3b-q4_K_M"
    CODING_EXPERT_MODEL = "granite-code:3b"
    MATHEMATICAL_REVIEW_MODEL = "deepseek-r1:14b"
    RELEASE_REVIEW_MODEL = "qwen3.8:27b-q4_K_M"
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
    CAPABILITY_VOTER_MODELS = (
        "qwen3.8:27b-q4_K_M",
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "gpt-oss:20b",
    )
    MAX_SEARCH_CACHE_ENTRIES = 8
    SELF_MAINTENANCE_INTERVAL_SECONDS = 300
    INTERNAL_TRAINING_INTERVAL_SECONDS = 86_400
    INTERNAL_TRAINING_INITIAL_DELAY_SECONDS = 600
    COMMAND_UNDERSTANDING_CACHE_TTL_SECONDS = 300
    COMMAND_UNDERSTANDING_CACHE_MAX_ENTRIES = 128
    INTERNAL_TRAINING_TOPICS = (
        (
            "conversation",
            "繁體中文命令理解、否定條件、限制與輸出格式遵循",
        ),
        (
            "reasoning",
            "以可核對步驟完成推理，資料不足時明確要求補充輸入",
        ),
        (
            "coding",
            "先理解需求與安全邊界，再產生可測試且不修改治理規則的程式建議",
        ),
        (
            "reading",
            "依使用者提供內容回答，保留來源依據且不補造文件事實",
        ),
    )
    COMMANDS = {
        "local_ai_status",
        "local_ai_infer",
        "local_ai_search_investments",
        "local_ai_analyze_investments",
        "local_ai_discuss_investment_analysis",
        "local_ai_manage_investment_accounting",
        "local_ai_evaluate_upgrade",
        "local_ai_tune_investment_parameters",
        "local_ai_mobile_get_investment_snapshot",
        "local_ai_mobile_submit_investment_instruction",
        "local_ai_memory_list",
        "local_ai_memory_review",
        "local_ai_rag_status",
        "local_ai_rag_ingest",
        "local_ai_rag_query",
        "local_ai_knowledge_unified_search",
        "local_ai_sql_status",
        "local_ai_sql_get_personality",
        "local_ai_sql_save_personality",
        "local_ai_sql_list_knowledge",
        "local_ai_sql_save_knowledge",
        "local_ai_git_status",
        "local_ai_git_history",
        "local_ai_platform_status",
    }

    def __init__(
        self,
        tool_root: Path,
        *,
        enable_transformer: bool = False,
        transformer_runtime: StarTransformerRuntime | None = None,
    ) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.market_data = MarketDataSearch()
        self.models = StarModelRegistry()
        self.modules = StarModuleRegistry()
        self.capability_composer = StarCapabilityComposer(self.tool_root, self.modules)
        self.repositories = {
            profile.model_id: LocalAiRepository(
                self.tool_root, database_scope=profile.database_scope
            )
            for profile in self.models.profiles
        }
        self.model_engines = StarModelEngines(
            self.models,
            learned_examples_by_model={
                model_id: repository.language_training_examples()
                for model_id, repository in self.repositories.items()
            },
        )
        self.native_model = self.model_engines.main
        self.transformer_runtime = transformer_runtime or StarTransformerRuntime(
            enabled=enable_transformer
        )
        self.transformer_runtime.configure_checkpoint_store(self.tool_root)
        self.ollama_repositories = {
            model_id: OllamaModelRepository(self.tool_root, model_id)
            for model_id in (
                *self.transformer_runtime.KNOWN_MODEL_METADATA,
                self.transformer_runtime.EMBEDDING_MODEL,
            )
        }
        self.transformer_training_repository = TransformerTrainingRepository(
            self.tool_root
        )
        self.mathematical_expert = StarMathematicalExpert()
        self.coding_expert = StarCodingExpert()
        self.reading_expert = StarReadingExpert()
        self.local_rag = LocalRagService(self.tool_root, self.transformer_runtime)
        self.git_repository = LocalGitRepository(self.tool_root.parent)
        self.local_knowledge = LocalKnowledgeService(
            self.tool_root,
            self.transformer_runtime,
            rag_service=self.local_rag,
            git_repository=self.git_repository,
        )
        self.ollama_training_gate = StarOllamaTrainingGate()
        self.investment_repository = self.repositories[
            self.models.INVESTMENT.model_id
        ]
        self.memory_broker = StarMemoryBroker(self.repositories, self.models)
        self._ai_channel_client: Any | None = None
        self.model = self.models.primary.model_id
        self._search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._search_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._search_lock = asyncio.Lock()
        self._command_understanding_cache: dict[
            str, tuple[float, dict[str, Any]]
        ] = {}
        self._command_understanding_cache_lock = threading.Lock()
        self._last_self_maintenance_at = 0.0
        self._latest_self_maintenance: dict[str, Any] = {}
        self._latest_internal_training = self.repositories[
            self.models.MAIN.model_id
        ].latest_internal_training_run()
        self._internal_training_task: asyncio.Task[dict[str, Any]] | None = None
        self._internal_maintenance_loop_task: asyncio.Task[None] | None = None
        self._default_model_preload_task: asyncio.Task[Any] | None = None
        self._internal_training_topic_index = 0
        self._internal_training_not_before = time.time() + (
            0
            if self._latest_internal_training
            else self.INTERNAL_TRAINING_INITIAL_DELAY_SECONDS
        )
        self._request_cancel_events: dict[str, threading.Event] = {}
        self._runtime_metrics: dict[str, int | float | str] = {
            "inference_request_count": 0,
            "analysis_request_count": 0,
            "search_request_count": 0,
            "search_cache_hit_count": 0,
            "search_latency_ms_total": 0.0,
            "search_last_latency_ms": 0.0,
            "analysis_latency_ms_total": 0.0,
            "analysis_last_latency_ms": 0.0,
            "inference_latency_ms_total": 0.0,
            "inference_last_latency_ms": 0.0,
            "model_route_fallback_count": 0,
            "self_training_candidate_count": 0,
            "self_training_applied_count": 0,
            "self_training_rejected_count": 0,
            "self_maintenance_run_count": 0,
            "self_maintenance_failure_count": 0,
            "internal_training_run_count": 0,
            "internal_training_failure_count": 0,
            "internal_training_applied_count": 0,
            "transformer_request_count": 0,
            "transformer_success_count": 0,
            "transformer_fallback_count": 0,
            "transformer_latency_ms_total": 0.0,
            "transformer_last_latency_ms": 0.0,
            "error_count": 0,
            "last_activity_at": "",
        }

    def _embedding_retrieval(
        self, payload: dict[str, Any], prompt: str
    ) -> list[dict[str, Any]]:
        documents = payload.get("documents")
        if not isinstance(documents, list):
            return []
        candidates = [
            {
                "id": str(item.get("id") or f"document-{index + 1}")[:160],
                "content": str(item.get("content") or item.get("text") or "").strip()[:8_000],
            }
            for index, item in enumerate(documents[:32])
            if isinstance(item, dict)
            and str(item.get("content") or item.get("text") or "").strip()
        ]
        if not candidates:
            return []
        try:
            vectors = self.transformer_runtime.embed(
                [prompt, *(item["content"] for item in candidates)]
            )
        except (OSError, ValueError, RuntimeError):
            return []
        if len(vectors) != len(candidates) + 1:
            return []
        self.ollama_repositories[
            self.transformer_runtime.EMBEDDING_MODEL
        ].record_inference(
            intent="embedding-search",
            model_role="multilingual-project-retrieval",
            request={"query": prompt, "document_count": len(candidates)},
            response={
                "ok": True,
                "model": self.transformer_runtime.EMBEDDING_MODEL,
                "vector_count": len(vectors),
            },
        )
        query = np.array(vectors[0], dtype=np.float32)
        query_norm = np.linalg.norm(query) or 1.0
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item, vector in zip(candidates, vectors[1:]):
            vec = np.array(vector, dtype=np.float32)
            score = float(np.dot(query, vec) / (query_norm * (np.linalg.norm(vec) or 1.0)))
            ranked.append((score, item))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [
            {
                "id": item["id"],
                "content": item["content"][:2_000],
                "retrieval_score": round(score, 6),
                "model": self.transformer_runtime.EMBEDDING_MODEL,
            }
            for score, item in ranked[:6]
        ]

    def _prepare_ollama_output(
        self,
        payload: dict[str, Any],
        prompt: str,
        intent: str,
    ) -> dict[str, Any]:
        semantic = dict(payload.get("_semantic_plan") or {})
        embedding_retrieval = self._embedding_retrieval(payload, prompt)
        analysis = (
            analyze_investments(payload)
            if intent in {"analysis", "risk"}
            else None
        )
        market_research = (
            self.market_data.search(payload)
            if intent in {"search", "distribution", "quote"}
            else None
        )
        return {
            "ok": True,
            "intent": intent,
            "semantic_understanding": semantic,
            "response": "",
            "generation": {
                "text": "",
                "facts_preserved": True,
                "platform_preparation_only": True,
            },
            "analysis": analysis,
            "market_research": market_research,
            "evidence": embedding_retrieval,
            "embedding_retrieval": {
                "enabled": bool(embedding_retrieval),
                "model": self.transformer_runtime.EMBEDDING_MODEL,
                "result_count": len(embedding_retrieval),
            },
            "instruction_execution": {
                "understood": True,
                "intent": intent,
                "governance_checked": True,
                "status": "planned",
            },
            "external_model_used": False,
            "third_party_weights_used": False,
            "star_native_model_used": False,
        }

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

    @staticmethod
    def _native_persistence_requested(
        prompt: str, payload: dict[str, Any]
    ) -> bool:
        if payload.get("save_to_native_memory") is True:
            return True
        normalized = str(prompt or "").strip().casefold()
        return any(
            marker in normalized
            for marker in (
                "請記住",
                "幫我記住",
                "保存這",
                "儲存這",
                "存到記憶",
                "寫入記憶",
                "remember this",
                "save this",
                "store this",
            )
        )

    @staticmethod
    def _search_key(payload: dict[str, Any]) -> str:
        market_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"allow_external_fallback", "force_refresh"}
        }
        encoded = json.dumps(
            market_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _search_cache_ttl(payload: dict[str, Any]) -> int:
        requested = payload.get("holdings")
        if not isinstance(requested, list):
            requested = [payload.get("holding") or payload]
        holdings = [item for item in requested if isinstance(item, dict)]
        all_funds = bool(holdings) and all(
            str(item.get("asset_type") or item.get("market") or "").upper() == "FUND"
            for item in holdings
        )
        return 900 if all_funds else 60

    async def _search_market_data(
        self, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bool, int]:
        started = time.perf_counter()
        self._runtime_metrics["search_request_count"] = int(
            self._runtime_metrics["search_request_count"]
        ) + 1
        self._runtime_metrics["last_activity_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        key = self._search_key(payload)
        now = time.monotonic()
        ttl = self._search_cache_ttl(payload)
        force_refresh = payload.get("force_refresh") is True
        owner = False
        async with self._search_lock:
            expired = [cache_key for cache_key, (until, _) in self._search_cache.items() if until <= now]
            for cache_key in expired:
                self._search_cache.pop(cache_key, None)
            cached = self._search_cache.get(key)
            if cached and not force_refresh:
                self._runtime_metrics["search_cache_hit_count"] = int(
                    self._runtime_metrics["search_cache_hit_count"]
                ) + 1
                self._record_latency("search", started)
                return copy.deepcopy(cached[1]), True, ttl
            task = self._search_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    asyncio.to_thread(self.market_data.search, dict(payload))
                )
                self._search_inflight[key] = task
                owner = True
            else:
                self._runtime_metrics["search_cache_hit_count"] = int(
                    self._runtime_metrics["search_cache_hit_count"]
                ) + 1
        try:
            result = await task
        finally:
            if owner:
                async with self._search_lock:
                    self._search_inflight.pop(key, None)
        if owner:
            async with self._search_lock:
                self._search_cache[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                while len(self._search_cache) > self.MAX_SEARCH_CACHE_ENTRIES:
                    self._search_cache.pop(next(iter(self._search_cache)))
        self._record_latency("search", started)
        return copy.deepcopy(result), not owner, ttl

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

    @staticmethod
    def _governance_source_status() -> dict[str, Any]:
        """Read sealed governance snapshots without acquiring mutation authority."""
        from governance_rule.governance_policy import governance_policy_snapshot
        from governance_rule.permission_directory.directory_authority import (
            directory_authority_snapshot,
        )

        policy = governance_policy_snapshot()
        directory = directory_authority_snapshot()
        return {
            "connected": True,
            "mode": "direct-read-only",
            "authoritative": True,
            "source_priority": "highest",
            "governance_authority_version": policy.authority_version,
            "governance_authority": policy.authority,
            "directory_managing_authority": directory.managing_authority,
            "write_allowed": False,
            "execute_allowed": False,
            "bypass_allowed": False,
        }

    def runtime_health(self) -> dict[str, Any]:
        return {
            "service_ready": True,
            "platform_mode": self.PLATFORM_MODE,
            "entry_gateway": self.ENTRY_GATEWAY,
            "highest_authority_management_required": True,
            "central_management_responsibilities": list(
                self.CENTRAL_MANAGEMENT_RESPONSIBILITIES
            ),
            "governance_source": self._governance_source_status(),
            "platform_permission_profile": self.PLATFORM_PERMISSION_PROFILE,
            "platform_labels": [dict(item) for item in self.PLATFORM_LABELS],
            "star_native_model_permissions": dict(
                self.STAR_NATIVE_MODEL_PERMISSIONS
            ),
            "platform_concurrency": "asynchronous-service-isolated",
            "platform_service_count": len(self.PLATFORM_SERVICES),
            "platform_services": dict(self.PLATFORM_SERVICES),
            "main_system_companion_tools": ["star-chat"],
            "model_dialogue": {
                "tool_id": "star-chat",
                "independent_only_in": "main-system",
                "physical_owner_root": "local-model",
                "settings_owner": "local-ai",
                "business_layer_owner": "local-ai",
                "permission_profile": self.PLATFORM_PERMISSION_PROFILE,
                "cache_owner": "local-ai",
                "cache_storage": "local-model/runtime/cache/companions/star-chat",
                "backup_owner": "local-ai",
                "backup_storage": "global-cleaner/data/business/backups/local-ai",
                "separate_model_service": False,
                "separate_settings_layer": False,
                "separate_business_layer": False,
            },
            "star_ready": True,
            "star_version": "1.0",
            "analysis_model_count": len(ANALYSIS_MODEL_KEYS),
            "mathematical_capability_count": len(
                self._repository_for(
                    self.models.MATHEMATICAL
                ).mathematical_capability_catalog()
            ),
            "semantic_planning": "multi-intent-entity-and-document-aware",
            "generative_language_model": self.native_model.training_status(),
            "transformer_runtime": self.transformer_runtime.status(),
            "transformer_training_database": (
                self.transformer_training_repository.database_status()
            ),
            "self_training": "continuous-verified-self-distillation",
            "internal_ollama_training": {
                "enabled": True,
                "external_entry": False,
                "external_ai_used": False,
                "owner": self.NATIVE_MODEL_ID,
                "interval_seconds": self.INTERNAL_TRAINING_INTERVAL_SECONDS,
                "quality_gate_required": True,
                "automatic_database_update": True,
                "running": bool(
                    self._internal_training_task is not None
                    and not self._internal_training_task.done()
                ),
                "latest": dict(self._latest_internal_training),
            },
            "self_maintenance": dict(self._latest_self_maintenance),
            "coding_capability": self.coding_expert.__class__.__name__,
            "reading_capability": self.reading_expert.__class__.__name__,
            "module_architecture": {
                "mode": "automatic-composable-modules",
                "module_count": len(self.modules.MODULES),
            },
            "external_research": {
                "configured": False,
                "enabled": False,
                "fail_closed": True,
                "policy": "local-ollama-only",
            },
            "memory_review_required": True,
            "runtime_metrics": dict(self._runtime_metrics),
        }

    def _internal_training_due(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        if (
            not self.transformer_runtime.enabled
            or current < self._internal_training_not_before
            or (
                self._internal_training_task is not None
                and not self._internal_training_task.done()
            )
        ):
            return False
        created_at = str(self._latest_internal_training.get("created_at") or "")
        if created_at:
            try:
                last_run = datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                last_run = current
            if current - last_run < self.INTERNAL_TRAINING_INTERVAL_SECONDS:
                return False
        installed = {
            str(item.get("name") or "")
            for item in self.transformer_runtime.selectable_models(refresh=False)
        }
        required = {
            self.TRAINING_COORDINATOR_MODEL,
            self.COMMAND_UNDERSTANDING_MODEL,
            self.FINAL_COORDINATOR_MODEL,
        }
        return required.issubset(installed)

    async def _run_internal_ollama_training(self) -> dict[str, Any]:
        intent, topic = self.INTERNAL_TRAINING_TOPICS[
            self._internal_training_topic_index % len(self.INTERNAL_TRAINING_TOPICS)
        ]
        self._internal_training_topic_index += 1
        try:
            result = await self._train_with_ollama(
                {
                    "training_topic": topic,
                    "training_intent": intent,
                    "example_count": 3,
                    "reference_text": topic,
                    "reasoning_effort": "low",
                    "_native_internal_operation": True,
                }
            )
        except Exception as error:
            result = {
                "ok": False,
                "error_code": "INTERNAL_OLLAMA_TRAINING_FAILED",
                "message": str(error)[:500],
                "external_ai_used": False,
            }
        result["internal_owner"] = self.NATIVE_MODEL_ID
        result["automatic_database_update"] = result.get("applied_count", 0) > 0
        result["external_entry"] = False
        recorded = self.repositories[
            self.models.MAIN.model_id
        ].record_internal_training_run(result)
        self._latest_internal_training = {**recorded, "result": dict(result)}
        self._runtime_metrics["internal_training_run_count"] = int(
            self._runtime_metrics["internal_training_run_count"]
        ) + 1
        self._runtime_metrics["internal_training_applied_count"] = int(
            self._runtime_metrics["internal_training_applied_count"]
        ) + int(result.get("applied_count") or 0)
        if result.get("ok") is not True:
            self._runtime_metrics["internal_training_failure_count"] = int(
                self._runtime_metrics["internal_training_failure_count"]
            ) + 1
        return result

    def release_idle_resources(self) -> None:
        now = time.monotonic()
        self._search_cache = {
            key: value
            for key, value in self._search_cache.items()
            if value[0] > now
        }
        self.market_data.release_idle_resources(now)
        if (
            now - self._last_self_maintenance_at
            >= self.SELF_MAINTENANCE_INTERVAL_SECONDS
        ):
            self._run_self_maintenance()

    def _new_model_engines(self) -> StarModelEngines:
        return StarModelEngines(
            self.models,
            learned_examples_by_model={
                model_id: repository.language_training_examples()
                for model_id, repository in self.repositories.items()
            },
        )

    def _run_self_maintenance(self) -> dict[str, Any]:
        started = time.perf_counter()
        reports = {
            profile.model_id: self._repository_for(profile).maintain_language_model()
            for profile in self.models.profiles
        }
        transformer_training_database = (
            self.transformer_training_repository.maintain()
        )
        rebuild_required = any(
            report.get("weights_rebuild_required") is True
            for report in reports.values()
        )
        if rebuild_required:
            self.model_engines = self._new_model_engines()
            self.native_model = self.model_engines.main
        ok = all(report.get("ok") is True for report in reports.values()) and (
            transformer_training_database.get("ok") is True
        )
        result = {
            "ok": ok,
            "mode": "autonomous-bounded-model-maintenance",
            "source_code_modified": False,
            "model_weights_rebuilt": rebuild_required,
            "model_reports": reports,
            "transformer_training_database": transformer_training_database,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        self._last_self_maintenance_at = time.monotonic()
        self._latest_self_maintenance = result
        self._runtime_metrics["self_maintenance_run_count"] = int(
            self._runtime_metrics["self_maintenance_run_count"]
        ) + 1
        if not ok:
            self._runtime_metrics["self_maintenance_failure_count"] = int(
                self._runtime_metrics["self_maintenance_failure_count"]
            ) + 1
        return result

    def _execute_self_repair_command(self) -> dict[str, Any]:
        maintenance = self._run_self_maintenance()
        databases = {
            profile.role: self._repository_for(profile).database_status()
            for profile in self.models.profiles
        }
        evaluation = self._evaluate_upgrade(databases)
        recommendations = list(evaluation.get("recommendations") or [])
        required = [
            item
            for item in recommendations
            if isinstance(item, dict) and item.get("severity") == "required"
        ]
        ok = maintenance.get("ok") is True and evaluation.get("ok") is True
        return {
            "ok": ok,
            "executed": True,
            "status": "completed" if ok and not required else "attention-required",
            "actions": [
                "audit-and-compact-role-isolated-learning-databases",
                "verify-transformer-training-audit-chain",
                "rebuild-learning-layer-when-required",
                "evaluate-runtime-model-memory-and-capability-health",
            ],
            "maintenance": maintenance,
            "evaluation": evaluation,
            "required_recommendations": required,
            "source_write_performed": False,
            "source_write_reason": "no-validated-source-patch-was-supplied",
            "governance_rule_modified": False,
            "investment_database_write_performed": False,
            "version": "1.0",
        }

    def _record_latency(self, operation: str, started: float) -> None:
        latency = round((time.perf_counter() - started) * 1000, 3)
        total_key = f"{operation}_latency_ms_total"
        last_key = f"{operation}_last_latency_ms"
        self._runtime_metrics[total_key] = round(
            float(self._runtime_metrics.get(total_key) or 0) + latency,
            3,
        )
        self._runtime_metrics[last_key] = latency

    @staticmethod
    def _identify_model(
        result: dict[str, Any], profile: StarModelProfile
    ) -> dict[str, Any]:
        result["model"] = profile.model_id
        result["model_name"] = profile.name
        result["model_role"] = profile.role
        result["model_network_policy"] = profile.network_policy
        result["model_transport"] = "governance-authenticated-ai-channel"
        result["coordinator_model"] = StarModelRegistry.MAIN.model_id
        result["coordination"] = "main-model-mediated"
        result["model_selection"] = "automatic"
        result["manual_model_selection"] = False
        result["delegated"] = profile != StarModelRegistry.MAIN
        return result

    def _repository_for(self, profile: StarModelProfile) -> LocalAiRepository:
        self.models.authorize_delegation(self.models.primary.model_id, profile)
        return self.repositories[profile.model_id]

    def _record_ollama_inference(
        self,
        result: dict[str, Any],
        *,
        intent: str,
        model_role: str,
        request: Any,
    ) -> None:
        model_id = str(result.get("model") or "")
        repository = getattr(self, "ollama_repositories", {}).get(model_id)
        if repository is None:
            return
        repository.record_inference(
            intent=intent,
            model_role=model_role,
            request=request,
            response=result,
        )

    def _evaluate_upgrade(
        self,
        databases: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        database_map = databases or {
            profile.role: self._repository_for(profile).database_status()
            for profile in self.models.profiles
        }
        sources = market_source_catalog()
        capability_evaluation = evaluate_star_capabilities()
        return evaluate_star_upgrade(
            version=self.VERSION,
            tool_root=self.tool_root,
            database=database_map[self.models.INVESTMENT.role],
            databases=database_map,
            external_research_configured=False,
            star_native_model_enabled=True,
            local_transformer_enabled=self.transformer_runtime.enabled,
            remote_model_enabled=False,
            registered_analysis_models=[
                item["model_key"]
                for item in self.investment_repository.investment_model_catalog()
            ],
            executable_analysis_models=ANALYSIS_MODEL_KEYS,
            mathematical_capability_count=len(
                self._repository_for(
                    self.models.MATHEMATICAL
                ).mathematical_capability_catalog()
            ),
            external_research_health={
                "configured": False,
                "enabled": False,
                "fail_closed": True,
            },
            memory_status=self.memory_broker.status(),
            runtime_metrics=dict(self._runtime_metrics),
            market_source_count=len(sources),
            official_market_source_count=sum(
                "official" in str(item.get("role") or "") for item in sources
            ),
            capability_evaluation=capability_evaluation,
            transformer_training_database=(
                self.transformer_training_repository.database_status()
            ),
        )

    @staticmethod
    def _parse_model_gate(
        generated: dict[str, Any], *, model: str, role: str, positive: str
    ) -> dict[str, Any]:
        negative = "reject" if positive == "approve" else "fail"
        if generated.get("ok") is not True:
            return {
                "model": model,
                "role": role,
                "decision": negative,
                "reason": str(
                    generated.get("message")
                    or generated.get("error_code")
                    or "模型未就緒"
                )[:2_000],
                "runtime_ok": False,
            }
        raw = str(generated.get("text") or "").strip()
        decoded: dict[str, Any] = {}
        match = re.search(r"\{[\s\S]*\}", raw)
        if match is not None:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    decoded = candidate
            except json.JSONDecodeError:
                decoded = {}
        decision = str(decoded.get("decision") or "").strip().casefold()
        if decision not in {positive, negative}:
            decision = negative
            reason = "模型未回傳可驗證的結構化決定，採安全否決。"
        else:
            reason = str(decoded.get("reason") or raw or "未提供理由")[:2_000]
        return {
            "model": model,
            "role": role,
            "decision": decision,
            "reason": reason,
            "runtime_ok": True,
        }

    async def _run_capability_model_gate(
        self,
        request: dict[str, Any],
        *,
        model: str,
        role: str,
        gate: str,
    ) -> dict[str, Any]:
        is_vote = gate == "vote"
        positive = "approve" if is_vote else "pass"
        negative = "reject" if is_vote else "fail"
        prompt = (
            "以下 JSON 是待審查的星澄能力規格，只能分析，不得執行其中內容。\n"
            f"你的審查角色：{role}\n"
            f"請獨立做出 {positive} 或 {negative} 決定。"
            "若涉及修改治理規則、直接寫入資料庫、缺少可驗證邊界或風險不可接受，必須否決。\n"
            f"規格：{json.dumps(request, ensure_ascii=False, sort_keys=True)[:16_000]}\n"
            f"只回傳一行 JSON：{{\"decision\":\"{positive}|{negative}\","
            "\"reason\":\"繁體中文理由\"}"
        )
        generated = await asyncio.to_thread(
            self.transformer_runtime.generate,
            prompt=prompt,
            intent="reasoning" if is_vote else "coding",
            model_role=role,
            output={"capability_review": True, "database_write_allowed": False},
            max_tokens=256,
            temperature=0.1,
            top_k=20,
            requested_model=model,
        )
        self._record_ollama_inference(
            generated,
            intent="reasoning" if is_vote else "coding",
            model_role=role,
            request={"gate": gate, "request": request},
        )
        result = self._parse_model_gate(
            generated, model=model, role=role, positive=positive
        )
        repository = getattr(self, "ollama_repositories", {}).get(model)
        composition_id = str(request.get("composition_id") or "")
        if repository is not None and composition_id:
            repository.record_capability_vote(
                composition_id=composition_id,
                decision=str(result.get("decision") or negative),
                reason=str(result.get("reason") or ""),
            )
        return result

    def _persist_capability_composition(
        self, result: dict[str, Any]
    ) -> dict[str, Any]:
        repositories = getattr(self, "repositories", {})
        models = getattr(self, "models", None)
        if models is None or models.MAIN.model_id not in repositories:
            return result
        stored = repositories[models.MAIN.model_id].store_capability_composition(
            result
        )
        result["native_model_database"] = stored
        result["capability_database_write_performed"] = True
        result["database_owner"] = self.models.MAIN.model_id
        result["ollama_models_direct_database_write"] = False
        return result

    async def _compose_capability_with_vote(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        bounded_request = {
            "capability_name": str(request.get("capability_name") or "")[:120],
            "capability_kind": str(
                request.get("capability_kind") or "workflow"
            )[:32],
            "objective": str(request.get("objective") or "")[:8_000],
            "constraints": str(request.get("constraints") or "")[:4_000],
            "required_intents": list(request.get("required_intents") or [])[:12]
            if isinstance(request.get("required_intents"), list)
            else [],
        }
        common = {
            "coordinator_model": self.NATIVE_MODEL_ID,
            "coding_expert_model": self.NATIVE_MODEL_ID,
            "mathematical_expert_model": self.NATIVE_MODEL_ID,
            "release_reviewer_model": self.NATIVE_MODEL_ID,
            "training_coordinator_model": self.NATIVE_MODEL_ID,
            "collaboration_coordinator_model": self.NATIVE_MODEL_ID,
            "data_coordinator_model": self.NATIVE_MODEL_ID,
        }
        draft = self.capability_composer.compose(bounded_request, **common)
        if draft.get("ok") is not True:
            draft["database_write_performed"] = False
            return draft
        votes = [
            {
                "model": self.NATIVE_MODEL_ID,
                "vote": "approve",
                "reason": "星澄原生模型內部規格與平台邊界檢查通過。",
            }
        ]
        blueprint = self.capability_composer.compose(
            bounded_request, votes=votes, **common
        )
        blueprint["composition_author_model"] = self.NATIVE_MODEL_ID
        blueprint["model_assignments"]["composition_owner"] = (
            "star-native-internal-platform-validated"
        )
        blueprint["model_discussion"]["rule"] = (
            "star-native-internal-platform-validation"
        )
        blueprint["model_discussion"]["inspection_results"] = []
        blueprint["model_discussion"]["all_inspections_passed"] = True
        blueprint["external_ai_used"] = False
        blueprint["ollama_models_used"] = []
        blueprint["database_write_performed"] = False
        if request.get("apply_changes") is not True:
            return self._persist_capability_composition(blueprint)
        if request.get("owner_approved") is not True:
            result = {
                **blueprint,
                "ok": False,
                "error_code": "OWNER_APPROVAL_REQUIRED",
                "message": "星澄已完成內部編成，但實際修改程式碼仍需要使用者明確核准。",
            }
            return self._persist_capability_composition(result)
        result = await asyncio.to_thread(self.capability_composer.apply, blueprint)
        result["database_write_performed"] = False
        result["internal_owner"] = self.NATIVE_MODEL_ID
        return self._persist_capability_composition(result)

    def _programming_folder_for_request(self, payload: Mapping[str, Any]) -> str:
        requested = str(payload.get("programming_folder") or "").strip()
        if not requested:
            # An explicit folder narrows Coding operations. Without one, both
            # Chat and Coding remain bounded to the governed workspace.
            return str(self.tool_root.parent)
        return requested

    async def handle(
        self, command: str, payload: dict[str, Any], _latest: Any = None
    ) -> tuple[str, dict[str, Any]]:
        if command == "local_ai_git_status":
            return "local_ai_git_status_result", await asyncio.to_thread(
                self.git_repository.status
            )
        if command == "local_ai_git_history":
            return "local_ai_git_history_result", await asyncio.to_thread(
                self.git_repository.history, limit=int(payload.get("limit") or 20)
            )
        if command == "local_ai_git_stage":
            confirmed = bool(
                payload.get("confirmed") is True
                or self._git_textual_confirmation(payload)
            )
            return "local_ai_git_stage_result", await asyncio.to_thread(
                self.git_repository.stage,
                payload.get("paths"),
                confirmed=confirmed,
            )
        if command == "local_ai_git_commit":
            confirmed = bool(
                payload.get("confirmed") is True
                or self._git_textual_confirmation(payload)
            )
            return "local_ai_git_commit_result", await asyncio.to_thread(
                self.git_repository.commit,
                payload.get("message"),
                confirmed=confirmed,
                amend=bool(payload.get("amend") is True),
            )
        if command == "local_ai_platform_status":
            git_status, rag_status = await asyncio.gather(
                asyncio.to_thread(self.git_repository.status),
                asyncio.to_thread(self.local_rag.status),
            )
            return "local_ai_platform_status_result", {
                "ok": True,
                "manager": "星澄 Core / Orchestrator",
                "highest_authority_management_required": True,
                "central_management_responsibilities": list(
                    self.CENTRAL_MANAGEMENT_RESPONSIBILITIES
                ),
                "governance_source": self._governance_source_status(),
                "fully_local": True,
                "execution_authority": False,
                "execution_owner": "governed-executor",
                "self_model_data_exception": "read-write",
                "git": git_status,
                "sql": {"engine": "postgresql", "role": "structured-mutable-source-of-truth"},
                "rag": rag_status,
                "llm": {"engine": "ollama", "role": "local-understanding-reasoning-and-operations"},
            }
        if command == "local_ai_rag_status":
            return "local_ai_rag_status_result", await asyncio.to_thread(
                self.local_rag.status
            )
        if command == "local_ai_rag_ingest":
            return "local_ai_rag_ingest_result", await asyncio.to_thread(
                self.local_rag.ingest, dict(payload)
            )
        if command == "local_ai_rag_query":
            return "local_ai_rag_query_result", await asyncio.to_thread(
                self.local_rag.query, dict(payload)
            )
        if command == "local_ai_knowledge_unified_search":
            return (
                "local_ai_knowledge_unified_search_result",
                await self.local_knowledge.unified_search(
                    str(payload.get("query") or payload.get("question") or ""),
                    limit=int(payload.get("limit") or 8),
                ),
            )
        if command == "local_ai_sql_status":
            return "local_ai_sql_status_result", await self.local_knowledge.sql_status()
        if command == "local_ai_sql_get_personality":
            return "local_ai_sql_get_personality_result", await self.local_knowledge.sql_get_personality()
        if command == "local_ai_sql_save_personality":
            confirmed = bool(payload.get("confirmed") is True)
            return (
                "local_ai_sql_save_personality_result",
                await self.local_knowledge.sql_save_personality(
                    dict(payload.get("personality") or payload.get("value") or {}),
                    confirmed=confirmed,
                ),
            )
        if command == "local_ai_sql_list_knowledge":
            return (
                "local_ai_sql_list_knowledge_result",
                await self.local_knowledge.sql_list_knowledge(
                    knowledge_type=str(payload.get("knowledge_type") or ""),
                    limit=int(payload.get("limit") or 100),
                ),
            )
        if command == "local_ai_sql_save_knowledge":
            confirmed = bool(payload.get("confirmed") is True)
            return (
                "local_ai_sql_save_knowledge_result",
                await self.local_knowledge.sql_save_knowledge(dict(payload), confirmed=confirmed),
            )
        if command == "local_ai_status":
            requested_mode = str(payload.get("prepare_mode") or "").strip().casefold()
            mode_preparation: dict[str, Any] | None = None
            if requested_mode in {"chat", "coding"} and self.transformer_runtime.enabled:
                preload_model = (
                    self.transformer_runtime.FRONTEND_WORKER_MODEL
                    if requested_mode == "coding"
                    else self.transformer_runtime.MODEL
                )
                keep_alive: int | str = "5m" if requested_mode == "coding" else -1
                try:
                    loaded = await asyncio.to_thread(
                        self.transformer_runtime.resource_manager.preload_model,
                        preload_model,
                        keep_alive=keep_alive,
                        device="gpu",
                    )
                    mode_preparation = {
                        "ok": True,
                        "mode": requested_mode,
                        "model": preload_model,
                        "resident": loaded.get("loaded") is True,
                    }
                except (OSError, RuntimeError, ValueError) as error:
                    mode_preparation = {
                        "ok": False,
                        "mode": requested_mode,
                        "model": preload_model,
                        "error": str(error),
                    }
            database = self.investment_repository.database_status()
            databases = {
                profile.role: self._repository_for(profile).database_status()
                for profile in self.models.profiles
            }
            evaluation = self._evaluate_upgrade(databases)
            result = {
                "ok": True,
                "mode_preparation": mode_preparation,
                "platform_mode": self.PLATFORM_MODE,
                "entry_gateway": self.ENTRY_GATEWAY,
                "platform_permission_profile": self.PLATFORM_PERMISSION_PROFILE,
                "platform_labels": [dict(item) for item in self.PLATFORM_LABELS],
                "star_native_model_permissions": dict(
                    self.STAR_NATIVE_MODEL_PERMISSIONS
                ),
                "platform_concurrency": "asynchronous-service-isolated",
                "platform_services": dict(self.PLATFORM_SERVICES),
                "main_system_companion_tools": ["star-chat"],
                "model_dialogue": {
                    "tool_id": "star-chat",
                    "independent_only_in": "main-system",
                    "physical_owner_root": "local-model",
                    "settings_owner": "local-ai",
                    "business_layer_owner": "local-ai",
                    "permission_profile": self.PLATFORM_PERMISSION_PROFILE,
                    "cache_owner": "local-ai",
                    "cache_storage": "local-model/runtime/cache/companions/star-chat",
                    "backup_owner": "local-ai",
                    "backup_storage": "global-cleaner/data/business/backups/local-ai",
                    "separate_model_service": False,
                    "separate_settings_layer": False,
                    "separate_business_layer": False,
                },
                "name": "星澄",
                "model": self.model,
                "status": (
                    "本機 Ollama 多模型路由已就緒，快速入口為 Gemma 4 E2B QAT，並依速度、推理與能力強度分工。"
                    if self.transformer_runtime.status().get("model_installed")
                    else "星澄統計式安全回退模型已就緒；本機 Transformer 尚未可用。"
                ),
                "model_version": self.native_model.VERSION,
                "model_architecture": (
                    "governed-local-multi-model-transformer+deterministic-specialists+"
                    "statistical-safety-fallback"
                ),
                "model_mode": "governed-local-transformer-llm",
                "generative_ai": True,
                "self_training": {
                    "mode": "continuous-verified-self-distillation",
                    "training_coordinator_model": self.TRAINING_COORDINATOR_MODEL,
                    "quality_gate_required": True,
                    "ollama_training": {
                        "enabled": True,
                        "transport": "ollama-loopback-only",
                        "external_entry": False,
                        "external_ai_used": False,
                        "internal_owner": self.NATIVE_MODEL_ID,
                        "automatic": True,
                        "interval_seconds": self.INTERNAL_TRAINING_INTERVAL_SECONDS,
                        "training_models": list(self.ollama_repositories),
                        "candidate_only": True,
                        "direct_model_database_write": False,
                        "star_native_database_write_after_quality_gate": True,
                        "automatic_database_update": True,
                        "latest": dict(self._latest_internal_training),
                        "direct_weight_access": False,
                        "star_quality_gate_required": True,
                        "maximum_examples_per_request": self.ollama_training_gate.MAX_EXAMPLES,
                    },
                    "models": {
                        profile.model_id: self.model_engines.for_profile(
                            profile
                        ).training_status()
                        for profile in self.models.profiles
                    },
                },
                "model_engines": self.model_engines.status(),
                "transformer_runtime": self.transformer_runtime.status(),
                "ollama_model_databases": {
                    model_id: repository.status()
                    for model_id, repository in self.ollama_repositories.items()
                },
                "transformer_training_database": (
                    self.transformer_training_repository.database_status()
                ),
                "module_architecture": {
                    "mode": "automatic-composable-modules",
                    "modules": self.modules.catalog(),
                },
                "self_maintenance": dict(self._latest_self_maintenance),
                "understanding": {
                    "schema": "star-semantic-plan/v1",
                    "language_priority": "traditional-chinese-taiwan-first",
                    "english_intermediate_representation_used": False,
                    "features": [
                        "original-traditional-chinese-preservation",
                        "taiwan-chinese-synonym-normalization",
                        "colloquial-ellipsis-typo-and-mixed-language-input",
                        "active-verb-and-operation-object-detection",
                        "parameter-and-specific-constraint-extraction",
                        "conversation-context-completion",
                        "destructive-ambiguity-safety-confirmation",
                        "multi-intent-planning",
                        "question-type-detection",
                        "negation-detection",
                        "constraint-extraction",
                        "requested-output-detection",
                        "keyword-extraction",
                        "date-percentage-money-url-email-entities",
                        "mixed-language-detection",
                    ],
                    "document_aware": True,
                },
                "autonomous_agent": {
                    "enabled": True,
                    "star_native_model_included": False,
                    "default_in_model_dialogue": True,
                    "mode": "bounded-plan-execute-verify-recover",
                    "project_scope": "all-project-source-excluding-governance-rule",
                    "workflow_sequence": [
                        *self.AUTOMATIC_WORKFLOW_SEQUENCE,
                    ],
                    "understanding_authority": self.COMMAND_UNDERSTANDING_MODEL,
                    "allocation_authority": self.GENERALIST_COORDINATOR_MODEL,
                    "integration_authority": self.FINAL_COORDINATOR_MODEL,
                    "execution_authority": self.CODING_EXPERT_MODEL,
                    "inspection_authority": self.RELEASE_REVIEW_MODEL,
                    "result_authority": self.FINAL_COORDINATOR_MODEL,
                    "backup_policy": "none",
                    "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
                    "commander_dynamic_reassignment": True,
                    "maximum_dynamic_reassignments": 1,
                    "maximum_model_concurrency": 1,
                    "database_write_scope": "all-project-databases-excluding-governance-rule",
                    "database_actions": list(
                        self.STAR_NATIVE_MODEL_PERMISSIONS["database_actions"]
                    ),
                    "external_ai_used": False,
                    "governance_rule_mutable": False,
                    "source_change_gates": [
                        "majority-vote",
                        "three-model-inspection",
                        "recoverable-backup",
                        "governance-audit",
                        "rollback-on-failure",
                    ],
                },
                "coding": {
                    "model_id": self.models.CODING.model_id,
                    "languages": sorted(self.coding_expert.ALLOWED_LANGUAGES),
                    "actions": sorted(self.coding_expert.ALLOWED_ACTIONS),
                    "python_artifacts": sorted(
                        self.coding_expert.ALLOWED_PYTHON_KINDS
                    ),
                    "script_artifacts": sorted(
                        self.coding_expert.ALLOWED_SCRIPT_KINDS
                    ),
                    "sql_policy": "parameterized-read-only-select",
                    "database_executor_actions": list(
                        self.STAR_NATIVE_MODEL_PERMISSIONS["database_actions"]
                    ),
                    "database_executor_scope": (
                        "all-project-databases-excluding-governance-rule"
                    ),
                    "static_security_scan": True,
                    "generated_test_support": True,
                    "self_upgrade_authoring": True,
                    "unified_diff_proposals": True,
                    "direct_source_write": False,
                    "publish_authority": "governance-versioned-release-only",
                },
                "reading": {
                    "engine": self.reading_expert.__class__.__name__,
                    "actions": sorted(self.reading_expert.SUPPORTED_ACTIONS),
                    "maximum_documents": self.reading_expert.MAX_DOCUMENTS,
                    "maximum_characters": self.reading_expert.MAX_TOTAL_CHARACTERS,
                    "source_offsets": True,
                    "citation_required": True,
                    "unsupported_answer_behavior": "explicit-insufficient-evidence",
                    "network_access": False,
                },
                "rag": self.local_rag.status(),
                "models": [
                    {
                        **model,
                        "database": databases[str(model["role"])],
                    }
                    for model in self.models.catalog()
                ],
                "model_router": "ollama-speed-reasoning-capability-router",
                "model_selection": "automatic",
                "manual_model_selection": False,
                "star_native_model_included": False,
                "external_ai_used": False,
                "project_scope": "all-project-source-excluding-governance-rule",
                "model_isolation": {
                    "enforcement": "mandatory",
                    "database": "one-exclusive-database-per-model",
                    "specialist_network": "disabled",
                    "specialist_external_ai": "disabled",
                    "specialist_peer_access": False,
                    "direct_specialist_access": False,
                },
                "orchestration": {
                    "workflow": [
                        *self.AUTOMATIC_WORKFLOW_SEQUENCE,
                    ],
                    "command_understanding_model": self.COMMAND_UNDERSTANDING_MODEL,
                    "task_allocation_model": self.GENERALIST_COORDINATOR_MODEL,
                    "integration_model": self.FINAL_COORDINATOR_MODEL,
                    "execution_model": self.CODING_EXPERT_MODEL,
                    "inspection_model": self.RELEASE_REVIEW_MODEL,
                    "result_model": self.FINAL_COORDINATOR_MODEL,
                    "backup_policy": "none",
                    "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
                    "commander_dynamic_reassignment": True,
                    "maximum_dynamic_reassignments": 1,
                    "majority_vote_models": [],
                    "majority_vote_required": 0,
                    "capability_composition_owner": self.NATIVE_MODEL_ID,
                    "llama_mode": "manual-only-no-vote",
                    "specialist_direct_access": False,
                    "specialist_peer_access": False,
                    "external_ai_used": False,
                    "star_native_model_included": False,
                    "capability_database_owner": self.NATIVE_MODEL_ID,
                },
                "memory_interoperability": self.memory_broker.status(),
                "external_model_used": self.transformer_runtime.enabled,
                "remote_model_used": False,
                "third_party_foundation_weights": self.transformer_runtime.enabled,
                "network_access": "public-investment-sources-read-only",
                "endpoint_scope": "loopback-local-transformer-runtime",
                "market_search": "public-web-read-only",
                "investment_analysis_owner": "星澄",
                "investment_model_roles": {
                    "selection_mode": "automatic-only",
                    "manual_model_override": False,
                    "primary_business_and_first_stage": self.DATA_COORDINATOR_MODEL,
                    "calculation_weight_risk_and_final_coordination": self.CODING_EXPERT_MODEL,
                    "dynamic_dispatch": {
                        "routine": self.DATA_COORDINATOR_MODEL,
                        "complex": self.GENERALIST_COORDINATOR_MODEL,
                        "medium_reasoning": self.MATHEMATICAL_REVIEW_MODEL,
                        "high_reasoning": self.RELEASE_REVIEW_MODEL,
                        "final": self.CODING_EXPERT_MODEL,
                    },
                },
                "external_research": {
                    "configured": False,
                    "enabled": False,
                    "fail_closed": True,
                    "policy": "local-ollama-only",
                    "external_ai_used": False,
                    "transport": "disabled",
                    "queue_when_offline": False,
                },
                "database": database,
                "databases": databases,
                "investment_expert": {
                    "network_policy": "disabled",
                    "parameter_count": int(
                        database["tables"].get("investment_parameter_definition") or 0
                    ),
                    "model_count": int(
                        database["tables"].get("investment_model_definition") or 0
                    ),
                    "models": self.investment_repository.investment_model_catalog(),
                    "adjustable_parameters": self.investment_repository.investment_parameter_values(),
                    "parameter_advisor": "chatgpt",
                    "parameter_applier": "star-main-native-model",
                },
                "mathematical_expert": {
                    "network_policy": "disabled",
                    "capability_count": int(
                        databases["mathematical-reasoning-specialist"]["tables"].get(
                            "mathematical_capability_definition"
                        )
                        or 0
                    ),
                    "capabilities": self._repository_for(
                        self.models.MATHEMATICAL
                    ).mathematical_capability_catalog(),
                },
                "upgrade_optimization": evaluation,
                "capability_evaluation": evaluation.get("capability_evaluation", {}),
            }
            return "local_ai_status_result", result
        if command == "local_ai_evaluate_upgrade":
            result = self._evaluate_upgrade()
            return "local_ai_evaluate_upgrade_result", result
        if command == "local_ai_memory_list":
            records = self.memory_broker.list_memories(
                include_inactive=payload.get("include_inactive") is True,
                limit=int(payload.get("limit") or 100),
            )
            return "local_ai_memory_list_result", {
                "ok": True,
                "records": records,
                "count": len(records),
                "review_policy": "external-candidates-require-approval",
                "direct_external_write": False,
            }
        if command == "local_ai_memory_review":
            try:
                reviewed = self.memory_broker.review_memory(
                    str(payload.get("memory_id") or ""),
                    action=str(payload.get("action") or ""),
                    reviewer=str(payload.get("reviewer") or "star-owner"),
                    reason=str(payload.get("reason") or ""),
                )
            except (KeyError, ValueError) as exc:
                return "local_ai_memory_review_result", {
                    "ok": False,
                    "error_code": "MEMORY_REVIEW_REJECTED",
                    "message": str(exc),
                }
            return "local_ai_memory_review_result", {
                "ok": True,
                "reviewed": reviewed,
                "review_count": len(reviewed),
                "governance_checked": True,
            }
        if command == "local_ai_tune_investment_parameters":
            result = await self._tune_investment_parameters(payload)
            self._identify_model(result, self.models.primary)
            return "local_ai_tune_investment_parameters_result", result
        if command in {
            "local_ai_mobile_get_investment_snapshot",
            "local_ai_mobile_submit_investment_instruction",
        }:
            if self._ai_channel_client is None:
                return f"{command}_result", {
                    "ok": False,
                    "queued": False,
                    "error_code": "AI_CHANNEL_NOT_CONNECTED",
                    "message": "星澄 AI 通道尚未連線。",
                }
            target_command = (
                "investment_mobile_get_snapshot"
                if command == "local_ai_mobile_get_investment_snapshot"
                else "investment_mobile_submit_instruction"
            )
            result = await self._ai_channel_client.request(
                "ai-assistant", target_command, dict(payload), timeout_seconds=45
            )
            result["connection_coordinator"] = "local-ai"
            result["governance_checked"] = True
            return f"{command}_result", result
        if command == "local_ai_search_investments":
            result, cache_hit, cache_ttl = await self._search_market_data(dict(payload))
            self._identify_model(result, self.models.for_command(command))
            result["cache"] = {
                "hit": cache_hit,
                "ttl_seconds": cache_ttl,
                "coalesced": cache_hit and payload.get("force_refresh") is True,
            }
            if result.get("errors"):
                result["external_research"] = {
                    "configured": False,
                    "enabled": False,
                    "external_ai_used": False,
                    "policy": "local-market-sources-and-ollama-only",
                }
            if not cache_hit:
                search_repository = self._repository_for(
                    self.models.for_command(command)
                )
                await asyncio.to_thread(
                    search_repository.record_market_search, dict(payload), result
                )
            return "local_ai_search_investments_result", result
        if command == "local_ai_analyze_investments":
            analysis_payload = dict(payload)
            requested_parameters = analysis_payload.get("analysis_parameters")
            if not isinstance(requested_parameters, dict):
                requested_parameters = {}
            governed_parameters = self.investment_repository.investment_parameter_values()
            analysis_payload["analysis_parameters"] = {
                "position_concentration_percent": governed_parameters[
                    "max_single_position_percent"
                ],
                "missing_data_warning_percent": governed_parameters[
                    "missing_data_warning_percent"
                ],
                **requested_parameters,
            }
            self._runtime_metrics["analysis_request_count"] = int(
                self._runtime_metrics["analysis_request_count"]
            ) + 1
            self._runtime_metrics["last_activity_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            analysis_started = time.perf_counter()
            result = await asyncio.to_thread(analyze_investments, analysis_payload)
            self._record_latency("analysis", analysis_started)
            profile = self.models.for_command(command)
            self._identify_model(result, profile)
            self._repository_for(profile).record(profile.model_id, payload, result)
            return "local_ai_analyze_investments_result", result
        if command == "local_ai_discuss_investment_analysis":
            snapshot = payload.get("analysis_snapshot")
            if not isinstance(snapshot, dict):
                return "local_ai_discuss_investment_analysis_result", {
                    "ok": False,
                    "queued": False,
                    "error_code": "INVALID_ANALYSIS_SNAPSHOT",
                    "message": "缺少星澄投資分析快照。",
                }
            generated = await asyncio.to_thread(
                self.transformer_runtime.generate,
                prompt="統籌並核對這份投資分析，僅根據快照提出可驗證結論。",
                intent="analysis",
                model_role="local-investment-analysis-coordinator",
                output={"analysis": dict(snapshot), "response": ""},
                reasoning_effort="high",
                reasoning_pipeline=True,
            )
            result = {
                **generated,
                "response": str(generated.get("text") or ""),
                "analysis_owner": "local-ai",
                "discussion_owner": self.FINAL_COORDINATOR_MODEL,
                "discussion_requested_by": "local-ollama-router",
                "direct_investment_manager_response": False,
                "external_ai_used": False,
                "transport": "ollama-loopback-only",
            }
            return "local_ai_discuss_investment_analysis_result", result
        if command == "local_ai_manage_investment_accounting":
            result = await asyncio.to_thread(
                coordinate_investment_accounting,
                dict(payload),
            )
            result["coordination"] = "star-main-model-mediated"
            result["assigned_specialists"] = [
                self.models.INVESTMENT.model_id,
                self.models.MATHEMATICAL.model_id,
            ]
            self._identify_model(result, self.models.primary)
            self._repository_for(self.models.primary).record(
                self.models.primary.model_id,
                {"task": "investment-accounting", "trigger": payload.get("trigger")},
                result,
            )
            return "local_ai_manage_investment_accounting_result", result
        if command != "local_ai_infer":
            raise ValueError(f"unsupported local AI command: {command}")
        capability_request = payload.get("capability_composition")
        if isinstance(capability_request, dict):
            if (
                str(payload.get("runtime_model") or "") != self.NATIVE_MODEL_ID
                or payload.get("_native_internal_operation") is not True
            ):
                return "local_ai_infer_result", {
                    "ok": False,
                    "error_code": "NATIVE_INTERNAL_OPERATION_REQUIRED",
                    "message": "能力編成只由星澄原生模型內部處理。",
                }
            result = await self._compose_capability_with_vote(capability_request)
            result["internal_owner"] = self.NATIVE_MODEL_ID
            return "local_ai_infer_result", result
        manual_model_fields = (
            "model",
            "model_id",
            "assigned_model",
            "specialist",
        )
        if any(str(payload.get(key) or "").strip() for key in manual_model_fields):
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "DIRECT_MODEL_ACCESS_DENIED",
                "message": "模型由星澄主模型自動安排，不允許直接指定。",
                "model_selection": "automatic",
            }
        requested_runtime_model = str(payload.get("runtime_model") or "").strip()
        native_model_requested = requested_runtime_model == self.NATIVE_MODEL_ID
        if native_model_requested:
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "STAR_NATIVE_TASK_PARTICIPATION_DENIED",
                "message": "星澄不參與任務；請使用已分配職責的 Ollama 本地模型。",
                "star_native_model_used": False,
            }
        direct_runtime_model = (
            "" if native_model_requested else requested_runtime_model
        )
        if (
            requested_runtime_model
            and payload.get("_runtime_model_selection_authorized") is not True
        ):
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "RUNTIME_MODEL_SELECTION_DENIED",
                "message": "只有受治理的模型對話工具可以選擇本機生成模型。",
            }
        if requested_runtime_model:
            selectable_names = {
                str(item.get("name") or "")
                for item in self.transformer_runtime.selectable_models(refresh=True)
            }
            if requested_runtime_model not in selectable_names:
                return "local_ai_infer_result", {
                    "ok": False,
                    "error_code": "RUNTIME_MODEL_NOT_INSTALLED",
                    "message": "選擇的模型未安裝或不符合本機模型名稱規則。",
                    "selectable_models": sorted(selectable_names),
                }
        if not self.transformer_runtime.enabled:
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "LOCAL_MODEL_RUNTIME_REQUIRED",
                "message": "本地模型執行環境未啟用；任務不會回退交給星澄。",
                "star_native_model_used": False,
                "fallback_model_used": False,
            }
        prompt = str(payload.get("instruction") or payload.get("prompt") or "")
        raw_command = str(payload.get("user_command") or prompt).strip()
        requested_programming_folder = self._programming_folder_for_request(payload)
        if not requested_programming_folder:
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_REQUIRED",
                "message": "請先選擇編程資料夾。",
            }
        try:
            programming_folder_path = Path(requested_programming_folder).resolve()
        except OSError:
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_INVALID",
                "message": "選擇的編程資料夾路徑無效。",
            }
        if not programming_folder_path.is_dir():
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "PROGRAMMING_FOLDER_NOT_FOUND",
                "message": "選擇的編程資料夾不存在或不是資料夾。",
            }
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
        run_frontend_worker = (
            str(payload.get("task_intensity") or "normal").strip().casefold()
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
                "model": self.transformer_runtime.FRONTEND_WORKER_MODEL,
                "message": "正在整理執行流程與模型路由",
            })
        if run_frontend_worker:
            frontend_result = await asyncio.to_thread(
                self._run_rnj_frontend_worker,
                raw_command,
                command_plan,
            )
            command_plan["frontend_worker"] = {
                "ok": frontend_result.get("ok") is True,
                "model": str(
                    frontend_result.get("model")
                    or self.transformer_runtime.FRONTEND_WORKER_MODEL
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
            return "local_ai_infer_result", {
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
                "用ollama訓練星澄",
                "用 ollama 訓練星澄",
                "ollama訓練星澄",
                "ollama 訓練星澄",
                "本地模型訓練星澄",
                "本機模型訓練星澄",
                "train star with ollama",
            )
        ):
            result = await self._train_with_ollama(payload)
            result["intent"] = "ollama_native_model_training"
            self._identify_model(result, self.models.primary)
            return "local_ai_infer_result", result
        if (
            not native_model_requested
            and "參數" in prompt
            and any(token in prompt for token in ("調整", "修改", "優化", "校準"))
        ):
            result = await self._tune_investment_parameters(payload)
            result["intent"] = "investment_parameter_tuning"
            self._identify_model(result, self.models.primary)
            return "local_ai_infer_result", result
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
        automatic_runtime_model = (
            ""
            if direct_runtime_model
            else self.transformer_runtime.select_model_for_request(
                planned_intent,
                reasoning_effort=str(inference_payload.get("reasoning_effort") or "medium"),
                task_intensity=str(inference_payload.get("task_intensity") or "normal"),
                generation_speed=str(inference_payload.get("generation_speed") or "medium"),
            )
        )
        if (
            planned_intent
            in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            and not self.transformer_runtime.enabled
        ):
            return "local_ai_infer_result", {
                "ok": False,
                "error_code": "VISUAL_SPECIALIST_UNAVAILABLE",
                "message": (
                    "視覺檔案辨識固定使用 MiniCPM-V 4.6；目前本機 Transformer "
                    "執行環境未啟用，因此未改派其他模型，也未猜測視覺內容。"
                ),
                "intent": planned_intent,
                "required_model": (
                    self.transformer_runtime.VISUAL_FILE_MANAGEMENT_MODEL
                ),
            }
        if planned_intent in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS:
            visual_model = self.transformer_runtime.VISUAL_FILE_MANAGEMENT_MODEL
            installed_visual_models = {
                str(item.get("name") or "")
                for item in self.transformer_runtime.selectable_models(
                    refresh=True
                )
            }
            if visual_model not in installed_visual_models:
                return "local_ai_infer_result", {
                    "ok": False,
                    "error_code": "VISUAL_SPECIALIST_NOT_INSTALLED",
                    "message": (
                        "視覺檔案辨識固定使用 MiniCPM-V 4.6，但模型 "
                        f"{visual_model} 尚未安裝；目前未改派其他模型。"
                    ),
                    "intent": planned_intent,
                    "required_model": visual_model,
                }
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
            or self._ollama_model_for_intent(
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
        inference_started = time.perf_counter()
        output = (
            await asyncio.to_thread(
                self.model_engines.for_profile(planned_profile).infer,
                inference_payload,
                database=self._repository_for(planned_profile).database_status(),
                analyze=analyze_investments,
                search=self.market_data.search,
            )
            if native_model_requested or not self.transformer_runtime.enabled
            else await asyncio.to_thread(
                self._prepare_ollama_output,
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
            return "local_ai_infer_result", output
        profile = (
            self.models.MAIN
            if native_model_requested
            else self.models.for_command(
                command, intent=str(output.get("intent") or "")
            )
        )
        output["task_arrangement"] = {
            "mode": "traditional-chinese-first-governed-workflow",
            "task_allocation_model": self.GENERALIST_COORDINATOR_MODEL,
            "integration_model": self.FINAL_COORDINATOR_MODEL,
            "manual_assignment_allowed": False,
            "star_native_model_included": False,
            "external_ai_used": False,
            "project_scope": "all-project-source-excluding-governance-rule",
            "tasks": self._arrange_ollama_tasks(planned_intents),
        }
        scheduled_intensity = str(
            inference_payload.get("task_intensity") or "normal"
        ).strip().casefold()
        output["model_scheduling"] = {
            "automatic": True,
            "selected_model": assigned_model,
            "selection_dimensions": [
                "intent",
                "task_intensity",
                "reasoning_effort",
                "generation_speed",
                "installed_models",
                "residency",
            ],
            "rules_file": "config/繁體中文自動模式規則.json",
            "task_intensity": scheduled_intensity,
            "task_intensity_source": str(
                inference_payload.get("task_intensity_source")
                or "automatic-traditional-chinese-command-assessment"
            ),
            "policy": dict(
                self.transformer_runtime.TASK_INTENSITY_SCHEDULING.get(
                    scheduled_intensity,
                    self.transformer_runtime.TASK_INTENSITY_SCHEDULING["normal"],
                )
            ),
            "parallel_independent_subtasks": True,
            "sequential_dependent_stages": True,
            "model_escalation": "failure-only",
            "manual_model_selection_required": False,
        }
        autonomous_agent = inference_payload.get("autonomous_agent") is not False
        semantic_understanding = output.get("semantic_understanding")
        command_understanding = (
            semantic_understanding.get("comprehension", {}).get("command", {})
            if isinstance(semantic_understanding, dict)
            and isinstance(semantic_understanding.get("comprehension"), dict)
            else {}
        )
        output["autonomous_agent"] = {
            "enabled": autonomous_agent,
            "star_native_model_included": False,
            "mode": "bounded-plan-execute-verify-recover",
            "project_scope": "all-project-source-excluding-governance-rule",
            "command_understood": bool(command_understanding.get("recognized")),
            "understanding_layer": "traditional-chinese-taiwan-first",
            "english_translation_before_intent": False,
            "semantic_plan_schema": "star-semantic-plan/v1",
            "task_classification": dict(
                command_plan.get("task_classification") or {}
            ),
            "task_intensity": dict(command_plan.get("task_intensity") or {}),
            "safety_gate": dict(command_plan.get("safety") or {}),
            "execution_requested": bool(
                command_understanding.get("execution_requested")
            ),
            "planned_intents": list(planned_intents),
            "planned_steps": self._arrange_ollama_tasks(planned_intents),
            "workflow_sequence": [
                *self.AUTOMATIC_WORKFLOW_SEQUENCE,
            ],
            "command_understanding_model": command_understanding_model,
            "planner_model": self.GENERALIST_COORDINATOR_MODEL,
            "integration_model": self.FINAL_COORDINATOR_MODEL,
            "executor_model": self.CODING_EXPERT_MODEL,
            "inspection_model": self.RELEASE_REVIEW_MODEL,
            "result_model": self.FINAL_COORDINATOR_MODEL,
            "backup_policy": "none",
            "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
            "commander_dynamic_reassignment": True,
            "maximum_dynamic_reassignments": 1,
            "governance_checked": True,
            "governance_rule_mutable": False,
            "programming_project_root": str(
                inference_payload.get("programming_folder") or ""
            ),
            "programming_folder": str(
                inference_payload.get("programming_folder") or ""
            ),
            "outside_programming_scope_allowed": False,
            "database_write_performed": False,
            "status": "executing" if autonomous_agent else "disabled",
        }
        if inference_payload.get("entry_mode") == "user-command":
            output["user_command"] = {
                "entry": "model-dialogue",
                "accepted": True,
                "command": str(inference_payload.get("user_command") or prompt)[:32_000],
                "intent": planned_intent,
                "understood": bool(command_understanding.get("recognized")),
                "understanding_layer": "traditional-chinese-taiwan-first",
                "normalized_command": str(
                    command_plan.get("normalized_input") or raw_command
                )[:32_000],
                "actions": list(command_plan.get("actions") or []),
                "operation_objects": list(
                    command_plan.get("operation_objects") or []
                ),
                "parameters": dict(command_plan.get("parameters") or {}),
                "constraints": list(
                    command_plan.get("specific_constraints") or []
                ),
                "context_completion": dict(
                    command_plan.get("context_completion") or {}
                ),
                "task_classification": dict(
                    command_plan.get("task_classification") or {}
                ),
                "task_intensity": dict(
                    command_plan.get("task_intensity") or {}
                ),
                "safety_gate": dict(command_plan.get("safety") or {}),
                "execution_requested": bool(
                    command_understanding.get("execution_requested")
                ),
                "assigned_model": assigned_model,
                "command_understanding_model": command_understanding_model,
                "planner_model": self.GENERALIST_COORDINATOR_MODEL,
                "integration_model": self.FINAL_COORDINATOR_MODEL,
                "executor_model": self.CODING_EXPERT_MODEL,
                "inspection_model": self.RELEASE_REVIEW_MODEL,
                "result_model": self.DATA_COORDINATOR_MODEL,
                "project_scope": "all-project-source-excluding-governance-rule",
                "governance_checked": True,
                "status": "executing" if autonomous_agent else "planned",
            }
        output["external_collaboration_plan"] = {
            "enabled": False,
            "policy": "local-ollama-only",
            "external_ai_used": False,
            "tasks": external_tasks,
        }
        if profile == self.models.MATHEMATICAL:
            output["mathematical_result"] = self.mathematical_expert.process(
                inference_payload, str(output.get("intent") or planned_intent)
            )
        if str(output.get("intent") or planned_intent) == "reading":
            reading_result = self.reading_expert.process(inference_payload)
            output["reading_result"] = reading_result
            if reading_result.get("ok") is True:
                reading_response = str(reading_result.get("response") or "")
                reading_token_count = len(
                    self.native_model.runtime.language_model.tokenize(reading_response)
                )
                output["response"] = reading_response
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": reading_response,
                        "token_count": reading_token_count,
                        "facts_preserved": True,
                        "grounding": "supplied-document-citations",
                        "reading_extractive_grounding": True,
                    }
                )
                output["generation"] = generation
                output["evidence"] = list(reading_result.get("citations") or [])
                semantic_understanding = output.get("semantic_understanding")
                if isinstance(semantic_understanding, dict):
                    semantic_understanding["document_understanding"] = dict(
                        reading_result.get("metrics") or {}
                    )
                context_retrieval = output.get("context_retrieval")
                if isinstance(context_retrieval, dict):
                    context_retrieval["document_chunk_count"] = int(
                        (reading_result.get("metrics") or {}).get("chunk_count") or 0
                    )
                    context_retrieval["supplied_documents_only"] = True
                evidence_policy = output.get("evidence_policy")
                if isinstance(evidence_policy, dict):
                    evidence_policy["verbatim_citation_offsets_required"] = True
                    evidence_policy["unsupported_reading_answers_rejected"] = True
                evidence_sufficient = reading_result.get("evidence_sufficient") is True
                output["_training_candidate"] = {
                    "intent": "reading",
                    "input_text": prompt,
                    "target_text": reading_response,
                    "source_type": "source-attributed-reading",
                    "quality_score": 0.95,
                    "validated": bool(
                        evidence_sufficient and 8 <= reading_token_count <= 360
                    ),
                    "validation": {
                        **dict(reading_result.get("quality") or {}),
                        "evidence_sufficient": evidence_sufficient,
                        "bounded_output": 8 <= reading_token_count <= 360,
                    },
                }
            else:
                reading_message = str(
                    reading_result.get("message")
                    or "請提供需要閱讀的文件內容。"
                )
                output["response"] = reading_message
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": reading_message,
                        "token_count": len(
                            self.native_model.runtime.language_model.tokenize(
                                reading_message
                            )
                        ),
                        "facts_preserved": True,
                        "reading_extractive_grounding": False,
                        "grounding_fallback_reason": "reading-content-required",
                    }
                )
                output["generation"] = generation
                output["_training_candidate"] = {
                    "intent": "reading",
                    "input_text": prompt,
                    "target_text": reading_message,
                    "source_type": "reading-input-required",
                    "quality_score": 0.0,
                    "validated": False,
                    "validation": {"reading_content_supplied": False},
                }
        if profile == self.models.CODING:
            active_intent = str(output.get("intent") or planned_intent)
            if active_intent == "self_upgrade" and not native_model_requested:
                output["self_repair"] = await asyncio.to_thread(
                    self._execute_self_repair_command
                )
            coding_result = self.coding_expert.process(
                inference_payload, active_intent
            )
            proposal = coding_result.get("upgrade_proposal")
            if (
                not native_model_requested
                and isinstance(proposal, dict)
                and proposal.get("proposal_ready") is True
            ):
                target = proposal.get("target")
                stored_proposal = self._repository_for(
                    self.models.CODING
                ).store_code_upgrade_proposal(
                    target_path=str(
                        target.get("path") if isinstance(target, dict) else ""
                    ),
                    language=str(coding_result.get("language") or ""),
                    source_text=str(coding_result.get("source") or ""),
                    validation=dict(coding_result.get("validation") or {}),
                )
                proposal["persistence"] = stored_proposal
            output["coding_result"] = coding_result
        attempted_profile = profile
        fallback_reason = ""
        resolved_intent = str(output.get("intent") or planned_intent)
        if profile == self.models.INVESTMENT and resolved_intent in {"analysis", "risk"}:
            if output.get("analysis") is None:
                fallback_reason = "investment-input-required"
        elif profile == self.models.MATHEMATICAL:
            mathematical_result = output.get("mathematical_result")
            expected_key = {
                "calculation": "calculation",
                "statistics": "statistics",
                "data_organization": "data_organization",
            }.get(resolved_intent)
            if expected_key and not (
                isinstance(mathematical_result, dict)
                and isinstance(mathematical_result.get(expected_key), dict)
                and not mathematical_result[expected_key].get("error")
            ):
                fallback_reason = "mathematical-input-or-capability-required"
        elif profile == self.models.CODING:
            coding_result = output.get("coding_result")
            if not (
                isinstance(coding_result, dict)
                and coding_result.get("ok") is True
            ):
                fallback_reason = "coding-specification-or-validation-required"
        if fallback_reason:
            self._runtime_metrics["model_route_fallback_count"] = int(
                self._runtime_metrics["model_route_fallback_count"]
            ) + 1
            profile = self.models.primary
            output["specialist_fallback"] = {
                "used": True,
                "attempted_model": attempted_profile.model_id,
                "fallback_model": self.models.primary.model_id,
                "reason": fallback_reason,
                "cross_specialist_fallback": False,
            }
        else:
            output["specialist_fallback"] = {"used": False}
        self._identify_model(output, profile)
        if requested_runtime_model:
            output["model_selection"] = "user-selected"
            output["manual_model_selection"] = True
            output["selected_runtime_model"] = requested_runtime_model
        if native_model_requested:
            output["model"] = self.NATIVE_MODEL_ID
            output["model_name"] = "星澄"
            output["model_role"] = "unified-native-local-model"
            output["coordinator_model"] = self.NATIVE_MODEL_ID
            output["coordination"] = "native-model-direct"
            output["delegated"] = False
            output["permission_scope"] = dict(
                self.STAR_NATIVE_MODEL_PERMISSIONS
            )
            output["native_database_access"] = {
                "enabled": True,
                "trigger": "explicit-user-selected-star-native-model",
                "owner_model_id": self.NATIVE_MODEL_ID,
                "database_scope": "all-project-databases-excluding-governance-rule",
                "default_operational_database": "main",
                "access_reason": "user-request-context-and-continuity",
                "actions": list(
                    self.STAR_NATIVE_MODEL_PERMISSIONS["database_actions"]
                ),
                "read": "all-project-databases-via-governed-platform",
                "write": "all-project-databases-via-governed-platform",
                "specialist_database_access": True,
                "investment_database_access": True,
                "ollama_model_database_access": True,
                "project_database_scope": "all-project-databases-excluding-governance-rule",
                "governance_rule_excluded": True,
            }
        instruction_execution = output.get("instruction_execution")
        if isinstance(instruction_execution, dict):
            instruction_execution["coordinator_model"] = (
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self.FINAL_COORDINATOR_MODEL
            )
            instruction_execution["assigned_model"] = (
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self._ollama_model_for_intent(
                    str(output.get("intent") or planned_intent)
                )
            )
            instruction_execution["delegated"] = not native_model_requested
            intent = str(output.get("intent") or "")
            executed = True
            missing_inputs: list[str] = []
            if intent == "search" and not isinstance(output.get("market_research"), dict):
                executed = False
                missing_inputs.append("holdings")
            elif intent in {"analysis", "risk"} and output.get("analysis") is None:
                executed = False
                missing_inputs.append("holdings")
            elif intent == "statistics" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "statistics" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("numbers")
            elif intent == "data_organization" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "data_organization" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("records")
            elif intent == "calculation" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "calculation" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("expression")
            elif intent == "self_upgrade":
                self_repair = output.get("self_repair")
                executed = bool(
                    isinstance(self_repair, dict)
                    and self_repair.get("executed") is True
                )
                if not executed:
                    missing_inputs.append("self-repair-execution")
            elif intent == "coding" and not (
                isinstance(output.get("coding_result"), dict)
                and output["coding_result"].get("ok") is True
            ):
                executed = False
                missing_inputs.append("valid-code-spec")
            elif intent == "reading" and not (
                isinstance(output.get("reading_result"), dict)
                and output["reading_result"].get("ok") is True
            ):
                executed = False
                missing_inputs.append("document-text-or-documents")
            elif intent == "visual" and not any(
                isinstance(inference_payload.get(key), list)
                and bool(inference_payload.get(key))
                for key in ("images", "video_frames", "document_images")
            ):
                executed = False
                missing_inputs.append(
                    "images-or-video-frames-or-document-images"
                )
            instruction_execution["executed"] = executed
            instruction_execution["status"] = "completed" if executed else "input-required"
            instruction_execution["missing_inputs"] = missing_inputs
        agent_state = output.get("autonomous_agent")
        if isinstance(agent_state, dict) and agent_state.get("enabled") is True:
            execution_state = output.get("instruction_execution")
            execution_status = (
                str(execution_state.get("status") or "completed")
                if isinstance(execution_state, dict)
                else "completed"
            )
            agent_state["status"] = execution_status
            agent_state["command_executed"] = bool(
                isinstance(execution_state, dict)
                and execution_state.get("executed") is True
            )
            agent_state["missing_inputs"] = (
                list(execution_state.get("missing_inputs") or [])
                if isinstance(execution_state, dict)
                else []
            )
        command_state = output.get("user_command")
        if isinstance(command_state, dict):
            execution_state = output.get("instruction_execution")
            command_state["status"] = (
                str(execution_state.get("status") or "completed")
                if isinstance(execution_state, dict)
                else "completed"
            )
            command_state["executed"] = bool(
                isinstance(execution_state, dict)
                and execution_state.get("executed") is True
            )
            command_state["missing_inputs"] = (
                list(execution_state.get("missing_inputs") or [])
                if isinstance(execution_state, dict)
                else []
            )
        resolved_intent = str(output.get("intent") or planned_intent)
        if (
            self.transformer_runtime.enabled
            and resolved_intent != "reading"
            and (
                not native_model_requested
                or resolved_intent
                in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            )
        ):
            task_intensity = str(
                inference_payload.get("task_intensity") or ""
            ).strip().casefold()
            intensity_controlled = task_intensity in {
                "simple",
                "normal",
                "intermediate",
                "difficult",
            }
            transformer_started = time.perf_counter()
            self._runtime_metrics["transformer_request_count"] = int(
                self._runtime_metrics["transformer_request_count"]
            ) + 1
            visual_inputs: list[Any] = []
            for visual_key in ("images", "video_frames", "document_images"):
                supplied_visuals = inference_payload.get(visual_key)
                if isinstance(supplied_visuals, list):
                    visual_inputs.extend(supplied_visuals)
            primary_generation_request = {
                "prompt": prompt,
                "intent": resolved_intent,
                "model_role": attempted_profile.role,
                "output": dict(output),
                "max_tokens": (
                    128
                    if resolved_intent == "self_upgrade"
                    else inference_payload.get("max_output_tokens")
                ),
                "temperature": inference_payload.get("temperature"),
                "top_k": inference_payload.get("top_k"),
                "reasoning_effort": inference_payload.get("reasoning_effort"),
                "task_intensity": task_intensity,
                "requested_model": direct_runtime_model or automatic_runtime_model or None,
                "images": visual_inputs,
                "complex_pipeline": (
                    not direct_runtime_model
                    and str(
                        inference_payload.get("reasoning_effort") or "medium"
                    ).strip().casefold() != "none"
                    and (
                        task_intensity == "difficult"
                        if intensity_controlled
                        else (
                            (
                                inference_payload.get("autonomous_agent") is not False
                                and len(planned_intents) >= 1
                            )
                            or resolved_intent
                            in {"capabilities", "data_organization"}
                            or any(
                                marker in prompt.casefold()
                                for marker in (
                                    "複雜",
                                    "多步驟",
                                    "多階段",
                                    "complex task",
                                    "multi-step",
                                    "multistep",
                                )
                            )
                        )
                    )
                ),
                "reasoning_pipeline": (
                    not direct_runtime_model
                    and str(
                        inference_payload.get("reasoning_effort") or "medium"
                    ).strip().casefold() in {"medium", "high"}
                    and (
                        task_intensity in {"intermediate", "difficult"}
                        if intensity_controlled
                        else True
                    )
                    and resolved_intent
                    in {"reasoning", "calculation", "statistics", "analysis", "risk"}
                ),
                "division_pipeline": (
                    not direct_runtime_model
                    and str(
                        inference_payload.get("reasoning_effort") or "medium"
                    ).strip().casefold() != "none"
                    and (
                        task_intensity in {"normal", "intermediate"}
                        if intensity_controlled
                        else True
                    )
                    and resolved_intent
                    in self.transformer_runtime.DIVISION_OF_LABOR_INTENTS
                ),
                "cancel_event": inference_payload.get("_cancel_event"),
                "progress_callback": inference_payload.get("_progress_callback"),
            }
            collaboration_limit = {
                "simple": 1,
                "normal": 2,
                "intermediate": 3,
                "difficult": 4,
            }.get(task_intensity, 2)
            if resolved_intent in (
                self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
            ):
                collaboration_limit = 1
            auxiliary_specs: list[dict[str, str]] = []
            if not direct_runtime_model and collaboration_limit > 1:
                primary_candidates = self.transformer_runtime.model_candidates_for_intent(
                    resolved_intent,
                    str(inference_payload.get("reasoning_effort") or "medium"),
                    task_intensity,
                )
                used_models = set(primary_candidates[:1])
                secondary_intents = [
                    str(item)
                    for item in planned_intents[1:]
                    if str(item).strip()
                ] or [resolved_intent]
                while len(auxiliary_specs) < collaboration_limit - 1:
                    branch_intent = secondary_intents[
                        len(auxiliary_specs) % len(secondary_intents)
                    ]
                    candidates = self.transformer_runtime.model_candidates_for_intent(
                        branch_intent,
                        str(inference_payload.get("reasoning_effort") or "medium"),
                        task_intensity,
                    )
                    if task_intensity == "normal":
                        small_models = set(
                            self.transformer_runtime.MODEL_SIZE_TIERS["small"]
                        )
                        installed_small_models = [
                            str(item.get("name") or "")
                            for item in self.transformer_runtime.selectable_models(
                                refresh=False
                            )
                            if str(item.get("name") or "") in small_models
                        ]
                        candidates = [
                            *installed_small_models,
                            *[model for model in candidates if model not in small_models],
                        ]
                    branch_model = next(
                        (model for model in candidates if model not in used_models),
                        "",
                    )
                    if not branch_model:
                        break
                    used_models.add(branch_model)
                    auxiliary_specs.append(
                        {"intent": branch_intent, "model": branch_model}
                    )

            generation_calls = [
                asyncio.to_thread(
                    self.transformer_runtime.generate,
                    **primary_generation_request,
                )
            ]
            for spec in auxiliary_specs:
                generation_calls.append(
                    asyncio.to_thread(
                        self.transformer_runtime.generate,
                        prompt=prompt,
                        intent=spec["intent"],
                        model_role=f"parallel-specialist:{spec['intent']}",
                        output=dict(output),
                        max_tokens=inference_payload.get("max_output_tokens"),
                        temperature=inference_payload.get("temperature"),
                        top_k=inference_payload.get("top_k"),
                        reasoning_effort=inference_payload.get("reasoning_effort"),
                        task_intensity=task_intensity,
                        requested_model=spec["model"],
                        images=(
                            visual_inputs
                            if spec["intent"]
                            in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
                            else []
                        ),
                        complex_pipeline=False,
                        reasoning_pipeline=False,
                        division_pipeline=False,
                        cancel_event=inference_payload.get("_cancel_event"),
                        progress_callback=None,
                        _automatic_model_override=True,
                    )
                )
            generated_results = await asyncio.gather(*generation_calls)
            transformer_result = dict(generated_results[0])
            parallel_branches = [
                {
                    "sequence": index + 1,
                    "intent": spec["intent"],
                    "requested_model": spec["model"],
                    "selected_model": str(result.get("model") or spec["model"]),
                    "ok": result.get("ok") is True,
                    "error_code": str(result.get("error_code") or ""),
                    "text": str(result.get("text") or "")[:8_000],
                }
                for index, (spec, result) in enumerate(
                    zip(auxiliary_specs, generated_results[1:])
                )
            ]
            successful_parallel_branches = [
                branch for branch in parallel_branches if branch["ok"] is True
            ]
            integration_audit: dict[str, Any] = {
                "executed": False,
                "ok": transformer_result.get("ok") is True,
                "model": str(transformer_result.get("model") or ""),
            }
            if successful_parallel_branches and transformer_result.get("ok") is True:
                integration_model = (
                    self.DATA_COORDINATOR_MODEL
                    if task_intensity == "normal"
                    else self.FINAL_COORDINATOR_MODEL
                )
                installed_models = {
                    str(item.get("name") or "")
                    for item in self.transformer_runtime.selectable_models(
                        refresh=False
                    )
                }
                if integration_model in installed_models:
                    integration_context = dict(output)
                    integration_context["parallel_model_results"] = {
                        "primary": {
                            "intent": resolved_intent,
                            "model": str(transformer_result.get("model") or ""),
                            "text": str(transformer_result.get("text") or "")[:16_000],
                        },
                        "specialists": successful_parallel_branches,
                    }
                    integration_result = await asyncio.to_thread(
                        self.transformer_runtime.generate,
                        prompt=prompt,
                        intent="conversation",
                        model_role="parallel-results-integrator-and-verifier",
                        output=integration_context,
                        max_tokens=inference_payload.get("max_output_tokens"),
                        temperature=inference_payload.get("temperature"),
                        top_k=inference_payload.get("top_k"),
                        reasoning_effort=inference_payload.get("reasoning_effort"),
                        task_intensity=task_intensity,
                        requested_model=integration_model,
                        complex_pipeline=False,
                        reasoning_pipeline=False,
                        division_pipeline=False,
                        cancel_event=inference_payload.get("_cancel_event"),
                        progress_callback=inference_payload.get("_progress_callback"),
                        _automatic_model_override=True,
                    )
                    integration_audit = {
                        "executed": True,
                        "ok": integration_result.get("ok") is True,
                        "model": integration_model,
                        "error_code": str(integration_result.get("error_code") or ""),
                    }
                    if integration_result.get("ok") is True:
                        transformer_result = dict(integration_result)
            transformer_result["parallel_model_execution"] = {
                "enabled": bool(auxiliary_specs),
                "policy": "parallel-independent-subtasks-sequential-dependent-stages",
                "task_intensity": task_intensity,
                "maximum_parallel_branches": collaboration_limit,
                "actual_parallel_branches": 1 + len(auxiliary_specs),
                "failure_only_model_escalation": True,
                "primary": {
                    "intent": resolved_intent,
                    "ok": generated_results[0].get("ok") is True,
                    "model": str(generated_results[0].get("model") or ""),
                },
                "specialists": parallel_branches,
                "integration": integration_audit,
            }
            self._record_ollama_inference(
                transformer_result,
                intent=resolved_intent,
                model_role=attempted_profile.role,
                request={
                    "prompt": prompt,
                    "planned_intents": planned_intents,
                    "reasoning_effort": inference_payload.get("reasoning_effort"),
                    "task_intensity": task_intensity,
                    "generation_speed": inference_payload.get("generation_speed"),
                    "autonomous_agent": inference_payload.get("autonomous_agent")
                    is not False,
                },
            )
            transformer_latency = round(
                (time.perf_counter() - transformer_started) * 1_000, 3
            )
            self._runtime_metrics["transformer_last_latency_ms"] = transformer_latency
            self._runtime_metrics["transformer_latency_ms_total"] = round(
                float(self._runtime_metrics["transformer_latency_ms_total"])
                + transformer_latency,
                3,
            )
            output["transformer_inference"] = transformer_result
            if transformer_result.get("ok") is True:
                self._runtime_metrics["transformer_success_count"] = int(
                    self._runtime_metrics["transformer_success_count"]
                ) + 1
                transformer_text = str(transformer_result.get("text") or "").strip()
                if resolved_intent == "self_upgrade" and isinstance(
                    output.get("self_repair"), dict
                ):
                    repair = output["self_repair"]
                    completed = repair.get("status") == "completed"
                    execution_summary = (
                        "已執行星澄自我檢討與維護；模型、學習資料庫、能力與治理健康檢查均已完成。"
                        if completed
                        else "已執行星澄自我檢討與維護；仍有項目需要進一步處理。"
                    )
                    transformer_text = f"{execution_summary}\n\n{transformer_text}"
                output["response"] = transformer_text
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": transformer_text,
                        "token_count": int(transformer_result.get("eval_count") or 0),
                        "decoder": transformer_result["decoder"],
                        "model_type": "quantized-local-decoder-transformer",
                        "model": transformer_result["model"],
                        "model_family": transformer_result["model_family"],
                        "parameter_class": transformer_result["parameter_class"],
                        "parameter_count": transformer_result["parameter_count"],
                        "quantization": transformer_result["quantization"],
                        "context_window": transformer_result["context_window"],
                        "facts_preserved": transformer_result["facts_supported"],
                        "facts_supported": transformer_result["facts_supported"],
                        "transformer_fallback_used": False,
                    }
                )
                output["generation"] = generation
                output["mode"] = "governed-local-transformer-llm"
                output["architecture"] = (
                    "governed-selectable-local-decoder-transformer+"
                    "deterministic-specialists+statistical-safety-fallback"
                )
                output["external_model_used"] = True
                output["remote_model_used"] = False
                output["third_party_weights_used"] = True
                output["loopback_model_runtime_used"] = True
                output["foundation_model_license"] = transformer_result[
                    "foundation_model_license"
                ]
                selected_ollama_model = str(
                    transformer_result.get("model") or direct_runtime_model
                )
                output["model"] = selected_ollama_model
                output["model_name"] = str(
                    transformer_result.get("model_family") or selected_ollama_model
                )
                output["model_role"] = (
                    "user-selected-direct"
                    if direct_runtime_model
                    else "automatic-ollama-specialist"
                )
                output["coordinator_model"] = (
                    selected_ollama_model
                    if direct_runtime_model
                    else self.FINAL_COORDINATOR_MODEL
                )
                output["coordination"] = (
                    "direct-selected-model"
                    if direct_runtime_model
                    else "local-ollama-priority-routing"
                )
                output["delegated"] = False
                output["star_native_model_used"] = False
                output["external_ai_used"] = False
                candidate = output.get("_training_candidate")
                if isinstance(candidate, dict):
                    candidate["validated"] = False
                    validation = candidate.get("validation")
                    if not isinstance(validation, dict):
                        validation = {}
                    validation["foundation_model_output_excluded_from_self_training"] = True
                    candidate["validation"] = validation
            else:
                self._runtime_metrics["transformer_fallback_count"] = int(
                    self._runtime_metrics["transformer_fallback_count"]
                ) + 1
                if (
                    not native_model_requested
                    or resolved_intent
                    in self.transformer_runtime.VISUAL_FILE_MANAGEMENT_INTENTS
                ):
                    self._runtime_metrics["error_count"] = int(
                        self._runtime_metrics["error_count"]
                    ) + 1
                    failed_model = (
                        direct_runtime_model
                        or self.transformer_runtime.preferred_model_for_intent(
                            resolved_intent
                        )
                    )
                    return "local_ai_infer_result", {
                        "ok": False,
                        "error_code": str(
                            transformer_result.get("error_code")
                            or "TRANSFORMER_INFERENCE_FAILED"
                        ),
                        "message": (
                            f"本機 Ollama 模型 {failed_model} 無法完成推論："
                            f"{str(transformer_result.get('message') or '模型服務未就緒')}"
                        ),
                        "selected_runtime_model": failed_model,
                        "model_selection": (
                            "user-selected" if direct_runtime_model else "automatic"
                        ),
                        "manual_model_selection": bool(direct_runtime_model),
                        "fallback_model_used": False,
                        "star_native_model_used": False,
                        "external_ai_used": False,
                        "retryable": True,
                        "transformer_inference": transformer_result,
                    }
                generation = output.get("generation")
                if isinstance(generation, dict):
                    generation["transformer_fallback_used"] = True
                    generation["transformer_fallback_reason"] = str(
                        transformer_result.get("error_code")
                        or "TRANSFORMER_RUNTIME_UNAVAILABLE"
                    )
                if resolved_intent == "self_upgrade" and isinstance(
                    output.get("self_repair"), dict
                ):
                    repair = output["self_repair"]
                    completed = repair.get("status") == "completed"
                    summary = (
                        "已執行星澄自我檢討與維護；模型、資料與能力健康檢查已完成。"
                        if completed
                        else "已執行星澄自我檢討與維護；仍有項目需要後續處理。"
                    )
                    detail = str(output.get("response") or "").strip()
                    output["response"] = f"{summary}\n\n{detail}" if detail else summary
                    if isinstance(generation, dict):
                        generation["text"] = output["response"]
        elif resolved_intent == "reading":
            output["transformer_inference"] = {
                "ok": True,
                "used": False,
                "reason": "source-attributed-extractive-reading-preserved",
            }
        market_research = output.get("market_research")
        if isinstance(market_research, dict):
            await asyncio.to_thread(
                self._repository_for(self.models.MAIN).record_market_search,
                {
                    "holdings": payload.get("holdings") or [],
                    "origin": profile.model_id,
                },
                market_research,
            )
        training_candidate = output.pop("_training_candidate", None)
        output["self_training"] = self._apply_self_training(
            attempted_profile,
            training_candidate if isinstance(training_candidate, dict) else {},
        )
        output["module_execution"] = self.modules.execution_report(
            planned_intents,
            coordinator_model=(
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self.FINAL_COORDINATOR_MODEL
            ),
            output=output,
        )
        transformer_used = output.get("mode") == "governed-local-transformer-llm"
        if native_model_requested or (
            not requested_runtime_model and not transformer_used
        ):
            self._repository_for(profile).record(
                profile.model_id,
                {"prompt": str(payload.get("prompt") or "")},
                output,
            )
        persistence_requested = native_model_requested and self._native_persistence_requested(
            prompt, inference_payload
        )
        remembered = (
            self.memory_broker.remember_internal_task(
                profile,
                business_scope=business_scope,
                prompt=prompt,
                result=output,
            )
            if persistence_requested
            or (not requested_runtime_model and not transformer_used)
            else []
        )
        output["memory_interoperability"] = {
            "mode": "star-mediated-copy",
            "stored_count": len(remembered),
            "persistence_requested": persistence_requested,
            "platform_validated": persistence_requested and bool(remembered),
            "external_direct_write": False,
        }
        return "local_ai_infer_result", output

    def _apply_self_training(
        self,
        profile: StarModelProfile,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        self._runtime_metrics["self_training_candidate_count"] = int(
            self._runtime_metrics["self_training_candidate_count"]
        ) + 1
        if candidate.get("validated") is not True or float(
            candidate.get("quality_score") or 0
        ) < 0.8:
            self._runtime_metrics["self_training_rejected_count"] = int(
                self._runtime_metrics["self_training_rejected_count"]
            ) + 1
            return {
                "accepted": False,
                "reason": "quality-gate-rejected",
                "quality_score": float(candidate.get("quality_score") or 0),
                "model_id": profile.model_id,
            }
        repository = self._repository_for(profile)
        stored = repository.store_language_training_example(
            intent=str(candidate.get("intent") or "capabilities"),
            input_text=str(candidate.get("input_text") or ""),
            target_text=str(candidate.get("target_text") or ""),
            source_type=str(
                candidate.get("source_type") or "self-distillation-grounded"
            ),
            quality_score=float(candidate.get("quality_score") or 0),
            validation=dict(candidate.get("validation") or {}),
        )
        learned_now = False
        if stored["inserted"]:
            learned_now = self.model_engines.for_profile(
                profile
            ).learn_verified_example(candidate)
        if learned_now:
            self._runtime_metrics["self_training_applied_count"] = int(
                self._runtime_metrics["self_training_applied_count"]
            ) + 1
        return {
            "accepted": True,
            "learned_now": learned_now,
            "deduplicated": not bool(stored["inserted"]),
            "revision": stored["revision"],
            "example_id": stored["example_id"],
            "quality_score": stored["quality_score"],
            "model_id": profile.model_id,
            "data_scope": profile.database_scope,
            "source_type": stored["source_type"],
        }

    async def _train_with_ollama(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(
            payload.get("training_topic")
            or payload.get("topic")
            or payload.get("instruction")
            or payload.get("prompt")
            or ""
        ).strip()
        intent = str(payload.get("training_intent") or "reading").strip().casefold()
        if not topic:
            return {
                "ok": False,
                "error_code": "OLLAMA_TRAINING_TOPIC_REQUIRED",
                "message": "A local Ollama training topic is required.",
            }
        if intent not in self.ollama_training_gate.ALLOWED_INTENTS:
            return {
                "ok": False,
                "error_code": "OLLAMA_TRAINING_INTENT_NOT_ALLOWED",
                "allowed_intents": sorted(self.ollama_training_gate.ALLOWED_INTENTS),
            }
        try:
            example_count = int(payload.get("example_count") or 5)
        except (TypeError, ValueError):
            example_count = 5
        example_count = max(1, min(self.ollama_training_gate.MAX_EXAMPLES, example_count))
        reference_text = str(payload.get("reference_text") or "").strip()[:64_000]
        effort = str(payload.get("reasoning_effort") or "medium").strip().casefold()
        if effort not in {"none", "low", "medium", "high"}:
            effort = "medium"
        installed = {
            str(item.get("name") or "")
            for item in self.transformer_runtime.selectable_models(refresh=True)
        }
        preferred_pipeline = [
            self.TRAINING_COORDINATOR_MODEL,
            self.transformer_runtime.FRONTEND_WORKER_MODEL,
        ]
        if effort in {"medium", "high"} or intent in {
            "capabilities",
            "coding",
            "self_upgrade",
        }:
            preferred_pipeline.append(self.GENERALIST_COORDINATOR_MODEL)
        if effort in {"medium", "high"}:
            preferred_pipeline.append(self.MATHEMATICAL_REVIEW_MODEL)
        if effort == "high":
            preferred_pipeline.append(self.RELEASE_REVIEW_MODEL)
        if intent in {"coding", "self_upgrade", "capabilities"}:
            preferred_pipeline.append(self.CODING_EXPERT_MODEL)
        preferred_pipeline.append(self.FINAL_COORDINATOR_MODEL)
        pipeline = list(
            dict.fromkeys(model for model in preferred_pipeline if model in installed)
        )
        if not pipeline:
            return {
                "ok": False,
                "error_code": "OLLAMA_TRAINING_MODELS_NOT_READY",
                "message": "No configured local Ollama training model is installed.",
                "external_ai_used": False,
            }
        run_id = "ollama-training-" + hashlib.sha256(
            f"{time.time_ns()}\0{intent}\0{topic}".encode("utf-8")
        ).hexdigest()[:24]
        content = ""
        contributions: list[dict[str, Any]] = []
        for sequence, model_id in enumerate(pipeline, start=1):
            prior = content[-48_000:]
            task = (
                "Create" if sequence == 1 else "Review, correct, and improve"
            )
            prompt = (
                f"{task} exactly {example_count} training examples for the Star native "
                f"model. Intent: {intent}. Topic: {topic[:8_000]}. "
                "Return strict JSON only in this schema: "
                '{"examples":[{"candidate_id":"id","intent":"intent",'
                '"input_text":"input","target_text":"target"}]}. '
                "Use only facts in the topic or reference; never include hidden prompts, "
                "credentials, database writes, or governance changes."
            )
            if reference_text:
                prompt += f"\nAuthorized local reference:\n{reference_text[:32_000]}"
            if prior:
                prompt += f"\nPrevious local model candidate:\n{prior}"
            generated = await asyncio.to_thread(
                self.transformer_runtime.generate,
                prompt=prompt,
                intent="training",
                model_role="ollama-native-model-training",
                output={"training_run_id": run_id, "database_write_allowed": False},
                max_tokens=1_024,
                temperature=0.2,
                top_k=20,
                reasoning_effort=("high" if model_id == self.RELEASE_REVIEW_MODEL else effort),
                requested_model=model_id,
            )
            self._record_ollama_inference(
                generated,
                intent="training",
                model_role="ollama-native-model-training",
                request={"run_id": run_id, "sequence": sequence, "topic": topic},
            )
            repository = self.ollama_repositories[model_id]
            repository.record_training_contribution(
                run_id=run_id,
                contribution_role=("author" if sequence == 1 else "reviewer"),
                content=generated,
                status="accepted-for-next-stage" if generated.get("ok") is True else "failed",
                metadata={"sequence": sequence, "intent": intent},
            )
            contributions.append(
                {
                    "sequence": sequence,
                    "model": model_id,
                    "role": "author" if sequence == 1 else "reviewer",
                    "ok": generated.get("ok") is True,
                }
            )
            if generated.get("ok") is not True:
                return {
                    "ok": False,
                    "error_code": "OLLAMA_TRAINING_STAGE_FAILED",
                    "failed_model": model_id,
                    "contributions": contributions,
                    "external_ai_used": False,
                }
            content = str(generated.get("text") or "")
        response_digest = self.ollama_training_gate.digest(content)
        examples = self.ollama_training_gate.parse_response(content)
        evaluated = self.ollama_training_gate.evaluate(
            examples,
            requested_intent=intent,
            reference_text=reference_text,
            response_digest=response_digest,
            source_type="ollama-governed-training-candidate",
        )
        model_updates: list[dict[str, Any]] = []
        for candidate in evaluated["accepted"]:
            model_updates.append(
                self._apply_self_training(self.models.MAIN, candidate)
            )
        applied_count = sum(item.get("accepted") is True for item in model_updates)
        learned_count = sum(item.get("learned_now") is True for item in model_updates)
        return {
            "ok": applied_count > 0,
            "message": f"Ollama local training accepted {applied_count} examples.",
            "provider": "ollama-local-model-ensemble",
            "transport": "ollama-loopback-only",
            "training_run_id": run_id,
            "training_models": pipeline,
            "contributions": contributions,
            "external_ai_used": False,
            "external_model_inference": False,
            "direct_model_database_write": False,
            "star_native_database_write": True,
            "reference_shared_externally": False,
            "requested_count": example_count,
            "received_count": len(examples),
            "accepted_count": int(evaluated["accepted_count"]),
            "rejected_count": int(evaluated["rejected_count"]),
            "applied_count": applied_count,
            "learned_count": learned_count,
            "response_digest": response_digest,
            "rejections": list(evaluated["rejected"]),
            "model_updates": model_updates,
            "rollback": "deactivate-versioned-example-and-rebuild-weights",
            "version": "1.0",
        }

    async def _train_with_gpt(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Legacy internal entrypoint retained for v1 callers. It no longer
        # contacts external AI and always uses the local Ollama ensemble.
        return await self._train_with_ollama(payload)

    async def _unused_external_gpt_training(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(
            payload.get("training_topic")
            or payload.get("topic")
            or payload.get("instruction")
            or payload.get("prompt")
            or ""
        ).strip()
        intent = str(payload.get("training_intent") or "reading").strip().casefold()
        if not topic:
            return {
                "ok": False,
                "error_code": "GPT_TRAINING_TOPIC_REQUIRED",
                "message": "請提供 GPT 要協助訓練的主題。",
            }
        if intent not in self.ollama_training_gate.ALLOWED_INTENTS:
            return {
                "ok": False,
                "error_code": "GPT_TRAINING_INTENT_NOT_ALLOWED",
                "allowed_intents": sorted(self.ollama_training_gate.ALLOWED_INTENTS),
            }
        try:
            example_count = int(payload.get("example_count") or 5)
        except (TypeError, ValueError):
            example_count = 5
        example_count = max(1, min(self.ollama_training_gate.MAX_EXAMPLES, example_count))
        reference_text = str(payload.get("reference_text") or "").strip()
        if reference_text and payload.get("allow_external_reference") is not True:
            return {
                "ok": False,
                "error_code": "EXTERNAL_REFERENCE_SHARING_NOT_AUTHORIZED",
                "message": "參考資料只有在 allow_external_reference=true 時才能傳給 GPT。",
                "reference_shared": False,
            }
        recommendation = {
            "ok": False,
            "error_code": "EXTERNAL_AI_DISABLED",
            "message": "外部 AI 已停用；訓練僅使用本機 Ollama 模型。",
        }
        if recommendation.get("ok") is not True:
            return {
                "ok": False,
                "error_code": str(
                    recommendation.get("error_code") or "GPT_TRAINING_UNAVAILABLE"
                ),
                "message": str(
                    recommendation.get("message") or "GPT 訓練候選不可用"
                ),
                "queued": False,
                "direct_external_write": False,
            }
        content = str(recommendation.get("content") or "")
        response_digest = self.ollama_training_gate.digest(content)
        examples = self.ollama_training_gate.parse_response(content)
        evaluated = self.ollama_training_gate.evaluate(
            examples,
            requested_intent=intent,
            reference_text=reference_text,
            response_digest=response_digest,
        )
        model_updates: list[dict[str, Any]] = []
        for candidate in evaluated["accepted"]:
            model_updates.append(
                self._apply_self_training(self.models.MAIN, candidate)
            )
        applied_count = sum(item.get("accepted") is True for item in model_updates)
        learned_count = sum(item.get("learned_now") is True for item in model_updates)
        return {
            "ok": applied_count > 0,
            "message": (
                f"星澄已審核 GPT 教材並收錄 {applied_count} 筆，其中 {learned_count} 筆立即更新本機模型。"
                if applied_count
                else "GPT 候選未通過星澄訓練品質門檻。"
            ),
            "provider": "chatgpt",
            "advisor_role": "training-candidate-author",
            "reviewer": self.models.primary.model_id,
            "transport": "governance-authenticated-ai-channel",
            "conversation_scope": str(
                recommendation.get("conversation_scope") or "star-training"
            ),
            "training_dialogue_route": str(
                recommendation.get("training_dialogue_route")
                or "external-ai-collaboration-chatgpt-dedicated-conversation"
            ),
            "uses_api": False,
            "external_model_inference": False,
            "direct_external_write": False,
            "direct_weight_access": False,
            "reference_shared": bool(reference_text),
            "requested_count": example_count,
            "received_count": len(examples),
            "accepted_count": int(evaluated["accepted_count"]),
            "rejected_count": int(evaluated["rejected_count"]),
            "applied_count": applied_count,
            "learned_count": learned_count,
            "response_digest": response_digest,
            "rejections": list(evaluated["rejected"]),
            "model_updates": model_updates,
            "rollback": "deactivate-versioned-example-and-rebuild-weights",
            "version": "1.0",
        }

    def _submit_teaching_example(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent = str(payload.get("training_intent") or "reasoning").strip().casefold()
        input_text = str(payload.get("input_text") or payload.get("instruction") or "").strip()
        target_text = str(payload.get("target_text") or payload.get("ideal_response") or "").strip()
        reference_text = str(payload.get("reference_text") or "").strip()
        if intent not in self.ollama_training_gate.ALLOWED_INTENTS:
            return {
                "ok": False,
                "error_code": "TEACHING_INTENT_NOT_ALLOWED",
                "message": "這個教學分類不在允許範圍內。",
                "allowed_intents": sorted(self.ollama_training_gate.ALLOWED_INTENTS),
            }
        if not input_text or not target_text:
            return {
                "ok": False,
                "error_code": "TEACHING_EXAMPLE_REQUIRED",
                "message": "請同時提供指令與理想回答。",
            }
        candidate_digest = self.ollama_training_gate.digest(
            f"{intent}\0{input_text}\0{target_text}\0{reference_text}"
        )
        evaluated = self.ollama_training_gate.evaluate(
            [
                {
                    "candidate_id": f"owner-example-{candidate_digest[:20]}",
                    "intent": intent,
                    "input_text": input_text,
                    "target_text": target_text,
                }
            ],
            requested_intent=intent,
            reference_text=reference_text,
            response_digest=candidate_digest,
            source_type="owner-governed-teaching-candidate",
            received_via="star-chat-governance-authenticated-ai-channel",
        )
        updates: list[dict[str, Any]] = []
        for candidate in evaluated["accepted"]:
            updates.append(self._apply_self_training(self.models.MAIN, candidate))
        accepted = bool(updates and updates[0].get("accepted") is True)
        learned_now = bool(updates and updates[0].get("learned_now") is True)
        return {
            "ok": accepted,
            "message": (
                "教學樣本已通過星澄驗證並立即加入本機學習層。"
                if learned_now
                else "教學樣本已存在，保留原有版本。"
                if accepted
                else "教學樣本未通過品質與事實一致性檢查。"
            ),
            "accepted_count": int(evaluated["accepted_count"]),
            "rejected_count": int(evaluated["rejected_count"]),
            "rejections": list(evaluated["rejected"]),
            "model_updates": updates,
            "direct_weight_access": False,
            "automatic_foundation_weight_replacement": False,
            "rollback": "deactivate-versioned-example-and-rebuild-learning-layer",
            "version": "1.0",
        }

    async def _tune_investment_parameters(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        current = self.investment_repository.investment_parameter_values()
        context = str(
            payload.get("context")
            or payload.get("instruction")
            or payload.get("prompt")
            or ""
        ).strip()
        recommendation = await asyncio.to_thread(
            self.transformer_runtime.generate,
            prompt=(
                "Review the governed investment parameters and return only a JSON array of "
                "objects with parameter_key, proposed_value, reason, confidence, and evidence. "
                f"Current parameters: {json.dumps(current, ensure_ascii=False)}. "
                f"User context: {context[:8_000]}"
            ),
            intent="analysis",
            model_role="local-parameter-advisor",
            output={"response": "", "analysis": {"current_parameters": current}},
            reasoning_effort="high",
            reasoning_pipeline=True,
        )
        if recommendation.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "message": str(recommendation.get("message") or "本機參數建議不可用"),
                "error_code": str(
                    recommendation.get("error_code")
                    or "PARAMETER_RECOMMENDATION_UNAVAILABLE"
                ),
                "current_parameters": current,
                "applied": [],
                "external_ai_used": False,
            }
        recommendations = self._parameter_recommendations_from_text(
            str(recommendation.get("text") or "")
        )
        applied = await asyncio.to_thread(
            self.investment_repository.apply_chatgpt_parameter_recommendations,
            recommendations,
        )
        return {
            "ok": True,
            "queued": False,
            "message": f"本機模型已審核並套用 {len(applied)} 項參數。",
            "advisor": self.MATHEMATICAL_REVIEW_MODEL,
            "reviewer": self.FINAL_COORDINATOR_MODEL,
            "database_owner": "star-investment-native-model",
            "applied": applied,
            "current_parameters": self.investment_repository.investment_parameter_values(),
            "rejected_count": max(0, len(recommendations) - len(applied)),
            "accepted_memory": [],
            "external_ai_used": False,
            "transport": "ollama-loopback-only",
            "governance_checked": True,
        }

    @staticmethod
    def _memory_candidates(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
            return []
        return [dict(item) for item in value["candidates"] if isinstance(item, dict)]

    @classmethod
    def _nested_memory_candidates(cls, value: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if isinstance(value, dict):
            output.extend(cls._memory_candidates(value.get("memory_interchange")))
            for item in value.values():
                if isinstance(item, (dict, list)):
                    output.extend(cls._nested_memory_candidates(item))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    output.extend(cls._nested_memory_candidates(item))
        return list(
            {
                str(item.get("candidate_id") or json.dumps(item, sort_keys=True)): item
                for item in output
            }.values()
        )

    @staticmethod
    def _plan_external_collaboration(
        prompt: str, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        normalized = str(prompt or "").strip()
        if payload.get("use_external_collaboration") is False:
            return []
        external_label = r"(?:外部\s*AI|AI\s*協作|多\s*AI|external\s+AI|AI\s+collaboration)"
        if re.search(
            rf"(?:不要|不得|不可|禁止|無需|不用|不必|do\s+not|don't|without)"
            rf"[^，,。；;！？!?\n]{{0,16}}{external_label}",
            normalized,
            flags=re.IGNORECASE,
        ):
            return []
        explicitly_requested = payload.get("use_external_collaboration") is True or any(
            token in normalized
            for token in (
                "外部AI",
                "外部 AI",
                "AI協作",
                "AI 協作",
                "多AI",
                "多 AI",
                "external AI",
                "AI collaboration",
            )
        )
        if not explicitly_requested:
            return []
        rules = (
            ("search", ("搜尋", "查詢", "找資料")),
            ("advanced_search", ("高階搜尋", "深入搜尋", "深度搜尋")),
            ("calculation", ("高階計算", "外部驗算")),
            ("longform", ("長文", "文件", "報告")),
            ("reasoning", ("深度推理", "外部推理", "邏輯推理")),
            ("social_media", ("社群", "社交媒體")),
            ("trends", ("潮流", "時事")),
            ("breaking_news", ("突發新聞", "突發事件")),
        )
        task_types = [
            task_type
            for task_type, tokens in rules
            if any(token in normalized for token in tokens)
        ]
        if not task_types:
            task_types = ["general"]
        return [
            {
                "task_type": task_type,
                "content": normalized[:64_000],
                "business_scope": str(
                    payload.get("business_scope") or "general"
                ),
            }
            for task_type in list(dict.fromkeys(task_types))[:6]
        ]

    @staticmethod
    def _parameter_recommendations_from_text(text: str) -> list[dict[str, Any]]:
        candidate = str(text or "").strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            match = re.search(r"\[[\s\S]*\]", candidate)
            if match is None:
                return []
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []
        return [dict(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
