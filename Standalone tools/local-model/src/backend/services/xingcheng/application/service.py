from __future__ import annotations

import asyncio
import json
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
from .training_gate import StarTrainingGate
from ..integration.memory_broker import StarMemoryBroker
from ..domain.model_registry import StarModelRegistry
from ..domain.module_registry import StarModuleRegistry
from ..domain.capability_composer import StarCapabilityComposer
from ..infrastructure.model_engines import StarModelEngines
from ..infrastructure.repository import LocalAiRepository
from ..infrastructure.fault_diagnostics import FaultDiagnostics
from ..infrastructure.native_runtime import StarNativeRuntime
from ..infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)
from ..integration.external_research import ExternalAiResearch
from .command_channels import CommandChannelsMixin
from .codex_diagnostics import CodexDiagnosticsMixin
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
from .xingcheng_shell import XingchengShellMixin


def _service_version() -> str:
    try:
        from shared_layer.registry.versioning import component_version

        return component_version("local-model")
    except Exception:
        pass
    manifest_path = Path(__file__).resolve().parents[5] / "manifest.json"
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"


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
    CodexDiagnosticsMixin,
    InvestmentChannelMixin,
    InferenceChannelMixin,
    XingchengShellMixin,
):
    VERSION = _service_version()
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
        {"id": "native-transformer", "label": "原生自訓模型"},
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
        "investment-manager": "automatic-local-native-routing",
        "programming": "hardware-stable-local-coding-owner",
        "reasoning": "native-model-reasoning-owner",
        "complex-work": "understand-allocate-role-integrate-execute-inspect-result",
        "data": "native-model-enterprise-investment-data",
        "native-training": "native-self-training",
        "capability-composition": "native-model-governed",
        "autonomous-agent": "traditional-chinese-first-governed-workflow",
        "star-native-model": "task-participation-denied",
    }
    NATIVE_RUNTIME_MODEL = "xingcheng-native-transformer"
    FINAL_COORDINATOR_MODEL = NATIVE_RUNTIME_MODEL
    TRAINING_COORDINATOR_MODEL = NATIVE_RUNTIME_MODEL
    DATA_COORDINATOR_MODEL = NATIVE_RUNTIME_MODEL
    COMMAND_UNDERSTANDING_MODEL = NATIVE_RUNTIME_MODEL
    FAST_COMMAND_UNDERSTANDING_MODEL = NATIVE_RUNTIME_MODEL
    GENERALIST_COORDINATOR_MODEL = NATIVE_RUNTIME_MODEL
    AUTONOMOUS_AGENT_MODEL = NATIVE_RUNTIME_MODEL
    CODING_EXPERT_MODEL = NATIVE_RUNTIME_MODEL
    MATHEMATICAL_REVIEW_MODEL = NATIVE_RUNTIME_MODEL
    RELEASE_REVIEW_MODEL = NATIVE_RUNTIME_MODEL
    AUTOMATIC_WORKFLOW_SEQUENCE = (
        "receive-original-traditional-chinese",
        "native-understand-command-and-normalize-taiwan-chinese",
        "native-analyze-code-stem-and-tool-calling-at-workflow-front",
        "extract-actions-objects-parameters-constraints",
        "classify-task-and-intensity",
        "apply-safety-and-permission-gates",
        "decompose-and-route-subtasks",
        "execute-and-collect-results",
        "cross-validate-repair-or-escalate",
        "integrate-localize-apply-verify-and-report",
    )
    CAPABILITY_VOTER_MODELS = (NATIVE_RUNTIME_MODEL,)
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
        (
            "analysis",
            "資料比較與趨勢解讀：只依據提供的數據下結論並標示不確定性",
        ),
        (
            "statistics",
            "統計量的正確使用：平均、中位數、標準差、百分比與樣本限制",
        ),
        (
            "calculation",
            "逐步算術與單位換算：列出計算過程並驗算結果一致性",
        ),
        (
            "data_organization",
            "資料分類、去重、排序與表格化摘要，保留原始數值不竄改",
        ),
        (
            "repair",
            "錯誤訊息判讀、根因假設排序與最小風險修復步驟",
        ),
        (
            "capabilities",
            "星澄可協助的事項與治理邊界：唯讀工具、需確認的寫入、禁止事項",
        ),
        (
            "risk",
            "投資與操作風險辨識：情境列舉、影響評估與緩解措施",
        ),
        (
            "self_upgrade",
            "提出受治理的系統修改提案：說明動機、影響範圍與驗證方式，不直接執行",
        ),
    )
    COMMANDS = {
        "xingcheng_chat",
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
        "xingcheng_diagnose_fault",
        "xingcheng_codex_alignment",
        "xingcheng_codex_mirror_check",
        "xingcheng_submit_teaching",
        "xingcheng_self_learning_cycle",
        "xingcheng_retention_sweep",
    }

    def __init__(
        self,
        tool_root: Path,
        *,
        enable_transformer: bool = True,
        native_runtime: StarNativeRuntime | None = None,
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
        self.native_runtime = native_runtime or StarNativeRuntime(
            enabled=enable_transformer
        )
        self.native_runtime.configure_checkpoint_store(self.tool_root)
        self.transformer_training_repository = TransformerTrainingRepository(
            self.tool_root
        )
        self.mathematical_expert = StarMathematicalExpert()
        self.coding_expert = StarCodingExpert()
        self.reading_expert = StarReadingExpert()
        self.local_rag = LocalRagService(self.tool_root, self.native_runtime)
        self.git_repository = LocalGitRepository(self.tool_root.parent)
        # Read-only fault-diagnosis evidence collector: governed codex
        # directories + main-system runtime state + outbox tail.
        self.fault_diagnostics = FaultDiagnostics(self.tool_root.parent)
        self.local_knowledge = LocalKnowledgeService(
            self.tool_root,
            self.native_runtime,
            rag_service=self.local_rag,
            git_repository=self.git_repository,
        )
        self.training_gate = StarTrainingGate()
        self.investment_repository = self.repositories[
            self.models.INVESTMENT.model_id
        ]
        self.memory_broker = StarMemoryBroker(self.repositories, self.models)
        self._ai_channel_client: Any | None = None
        # Governed external-research client (A58): reaches the web only via
        # the ai-channel route into ai-collaboration's embedded browser —
        # never a direct socket. Bound in bind_channel() with the service's
        # own channel client.
        self.external_research = ExternalAiResearch()
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
        # §1.1/A554：self-learning 週期由 main-system 經受管 system channel
        # 排程觸發；claim lease 到期重排或連續 tick 可能造成重複請求，
        # 此鎖保證同一行程內任一時刻只有一個 run_cycle 在跑。
        self._self_learning_cycle_lock = threading.Lock()
        # §10.67：retention sweep 重入鎖（與訓練鎖分離——保留清理不阻塞
        # 訓練互斥語意，但同行程仍只允許一輪）。
        self._retention_sweep_lock = threading.Lock()
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
            "self_training_pairs_recorded_count": 0,
            "self_training_pairs_completed_count": 0,
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
