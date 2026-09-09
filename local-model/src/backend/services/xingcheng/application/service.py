from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from ..infrastructure.market_data import MarketDataSearch
from .mathematical_expert import StarMathematicalExpert
from .coding_expert import StarCodingExpert
from .reading_expert import StarReadingExpert
from .local_rag import LocalRagService
from .local_knowledge import LocalKnowledgeService
from ..infrastructure.git_repository import LocalGitRepository
from .gpt_training_gate import StarOllamaTrainingGate
from ..integration.memory_broker import StarMemoryBroker
from ..domain.model_registry import StarModelRegistry
from ..domain.module_registry import StarModuleRegistry
from ..domain.capability_composer import StarCapabilityComposer
from ..infrastructure.model_engines import StarModelEngines
from ..infrastructure.repository import LocalAiRepository
from ..infrastructure.ollama_model_repository import OllamaModelRepository
from ..infrastructure.transformer_runtime import StarTransformerRuntime
from ..infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)
from .command_channels import CommandChannelsMixin
from .inference_channel import InferenceChannelMixin
from .investment_channel import InvestmentChannelMixin
from .local_ai_embedding import LocalAiEmbeddingMixin
from .local_ai_command import LocalAiCommandMixin
from .local_ai_market import LocalAiMarketMixin
from .local_ai_lifecycle import LocalAiLifecycleMixin
from .local_ai_health import LocalAiHealthMixin
from .local_ai_training import LocalAiTrainingMixin
from .local_ai_maintenance import LocalAiMaintenanceMixin
from .local_ai_utility import LocalAiUtilityMixin
from .local_ai_capability import LocalAiCapabilityMixin
from .local_ai_teaching import LocalAiTeachingMixin


class LocalAiService(
    LocalAiLifecycleMixin,
    LocalAiEmbeddingMixin,
    LocalAiCommandMixin,
    LocalAiMarketMixin,
    LocalAiHealthMixin,
    LocalAiTrainingMixin,
    LocalAiMaintenanceMixin,
    LocalAiUtilityMixin,
    LocalAiCapabilityMixin,
    LocalAiTeachingMixin,
    CommandChannelsMixin,
    InvestmentChannelMixin,
    InferenceChannelMixin,
):
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
        "database_write_scope": "xingcheng-model-internal-excluding-permission-data",
        "database_actions": (
            "read",
            "create-write-save",
            "append",
            "update",
            "delete",
            "rollback",
        ),
        "investment_database_write": True,
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
        "xingcheng_status",
        "xingcheng_infer",
        "xingcheng_search_investments",
        "xingcheng_analyze_investments",
        "xingcheng_discuss_investment_analysis",
        "xingcheng_manage_investment_accounting",
        "xingcheng_evaluate_upgrade",
        "xingcheng_tune_investment_parameters",
        "xingcheng_mobile_get_investment_snapshot",
        "xingcheng_mobile_submit_investment_instruction",
        "xingcheng_memory_list",
        "xingcheng_memory_review",
        "xingcheng_rag_status",
        "xingcheng_rag_ingest",
        "xingcheng_rag_query",
        "xingcheng_knowledge_unified_search",
        "xingcheng_sql_status",
        "xingcheng_sql_get_personality",
        "xingcheng_sql_save_personality",
        "xingcheng_sql_list_knowledge",
        "xingcheng_sql_save_knowledge",
        "xingcheng_git_status",
        "xingcheng_git_history",
        "xingcheng_platform_status",
    }

    def __init__(
        self,
        tool_root: Path,
        *,
        enable_transformer: bool = True,
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
