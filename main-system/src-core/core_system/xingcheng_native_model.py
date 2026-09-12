"""xingcheng_native_model — 星澄原生模型能力（重組收斂）.

星澄的原生模型能力定義其模型推理權限、能力分類、模型分配策略、
各業務能力面（投資、程式、連線、文件等）。

能力來源（收攏前分散在 manifest 多處）：
  * Governance Codex                      — 星澄的可行權力與禁止權力
  * manifest.capabilities.xingcheng       — 核心能力（入口、權限、工作流、模型分配）
  * manifest.capabilities.investment-*    — 投資能力面
  * manifest.capabilities.upgrade-*       — 程式升級能力面
  * manifest.capabilities.ai-connections  — AI 連線能力面

模型收斂為三個模式：
  1. ``main_model``       — 主模型（綜合能力：協調、理解、整合、檢查、裁決）
  2. ``chat_mode``        — 聊天模式（對話、理解、回應、文件閱讀、視覺辨識）
  3. ``programming_mode`` — 編程模式（程式碼執行、生成、分析、重構）

此模組為唯讀協調層，不執行任何 AI/模型推理（推理由 governed-executor 執行）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX

from .xingcheng_personality import XINGCHENG_MODULE_ID

_XINGCHENG_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "xingcheng"),
    None,
)
if _XINGCHENG_SOVEREIGN is None:
    raise RuntimeError("xingcheng sovereign not found in Governance Codex")

XINGCHENG_KIND = "local-native-model"

# 星澄的可行權力（來自 Codex）— 模型能力面
XINGCHENG_EMPOWERED_POWERS = _XINGCHENG_SOVEREIGN.powers
XINGCHENG_PROHIBITED_POWERS = _XINGCHENG_SOVEREIGN.prohibitions

# 預設模型能力權限
_DEFAULT_MODEL_PERMISSIONS: dict[str, bool] = {
    "inference": True,
    "understanding": True,
    "advisory": True,
    "generation": True,
    "entry_dispatch": False,
}

# ======================================================================
# 三模式收斂定義
# ======================================================================

# 主模型 — 綜合能力（協調、理解、整合、檢查、裁決、分配）
# 來自 manifest.capabilities.xingcheng 中的多個 *_owner / *_authority
_MAIN_MODEL_ID = "qwen3.8:27b-q4_K_M"
_MAIN_MODEL_RESPONSIBILITIES = (
    "coordination",
    "collaboration",
    "generalist",
    "understanding",
    "allocation",
    "integration",
    "inspection",
    "result",
    "failure-adjudication",
    "final-coordination",
)

# 聊天模式 — 對話、理解、回應、文件閱讀、視覺辨識
_CHAT_MODE_MODEL_ID = "qwen3.8:27b-q4_K_M"
_CHAT_MODE_DOCUMENT_READING_MODEL = "gemma4:12b-it-qat"
_CHAT_MODE_VISUAL_MODEL = "openbmb/minicpm-v4.6:q8_0"
_CHAT_MODE_RESPONSIBILITIES = (
    "language-understanding",
    "response-generation",
    "document-reading",
    "visual-recognition",
    "conversation",
)

# 編程模式 — 程式碼執行、生成、分析、重構
_PROGRAMMING_MODE_MODEL_ID = "qwen3.6:35b-a3b-coding"
_PROGRAMMING_MODE_RESPONSIBILITIES = (
    "coding-execution",
    "code-generation",
    "code-analysis",
    "refactoring",
    "self-upgrade",
)


class XingchengNativeModel:
    """星澄原生模型能力 — 收攏所有分散的能力定義，收斂為三個模型模式。

    模型模式：
      * main_model       — 主模型（綜合能力）
      * chat_mode        — 聊天模式（對話/文件/視覺）
      * programming_mode — 編程模式（程式碼）

    能力面：
      * core             — 核心模型權限與身份
      * capabilities     — 能力分類與模組清單
      * model_modes      — 三模式收斂（取代舊的扁平 model_registry）
      * business_domains — 各業務能力面（投資/程式/連線/文件）

    從 manifest 載入能力設定，從 Codex 載入權力邊界。
    此類別為唯讀快照，不執行模型推理。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._manifest_cache: dict[str, Any] | None = None

    # ==================================================================
    # 1. 核心模型權限與身份 (core)
    # ==================================================================

    @property
    def kind(self) -> str:
        """模型種類（本地原生模型）。"""
        return XINGCHENG_KIND

    @property
    def module_id(self) -> str:
        """所屬模組 ID。"""
        return XINGCHENG_MODULE_ID

    @property
    def native_model_authority(self) -> str:
        """原生模型權限等級。"""
        manifest = self._manifest()
        return str(
            manifest.get("star_native_model_authority")
            or "highest-permission-under-governance-rule-central-data-management"
        )

    @property
    def model_permissions(self) -> dict[str, bool]:
        """星澄原生模型的推理權限。"""
        xingcheng_caps = self._xingcheng_capabilities()
        permissions = xingcheng_caps.get("star_native_model_permissions") or {}
        if not isinstance(permissions, dict):
            return dict(_DEFAULT_MODEL_PERMISSIONS)
        merged = dict(_DEFAULT_MODEL_PERMISSIONS)
        for key, value in permissions.items():
            merged[str(key)] = bool(value)
        return merged

    @property
    def empowered_powers(self) -> tuple[str, ...]:
        """Codex 授權的可行權力。"""
        return XINGCHENG_EMPOWERED_POWERS

    @property
    def prohibited_powers(self) -> tuple[str, ...]:
        """Codex 禁止的權力。"""
        return XINGCHENG_PROHIBITED_POWERS

    @property
    def fixed_responsibilities(self) -> list[str]:
        """星澄的固定職責（SQL/RAG/Git 中央管理）。"""
        manifest = self._manifest()
        return list(manifest.get("star_fixed_responsibilities") or [])

    @property
    def entry_ownership(self) -> str:
        """AI 入口所有權。"""
        xingcheng_caps = self._xingcheng_capabilities()
        return str(xingcheng_caps.get("entry_ownership") or "all-ai-business-entries")

    @property
    def entry_sources(self) -> list[str]:
        """AI 入口來源清單。"""
        xingcheng_caps = self._xingcheng_capabilities()
        sources = xingcheng_caps.get("entry_sources") or []
        return list(sources) if isinstance(sources, list) else []

    @property
    def specialist_direct_access(self) -> bool:
        """是否允許直接存取專家模型。"""
        xingcheng_caps = self._xingcheng_capabilities()
        return bool(xingcheng_caps.get("specialist_direct_access") or False)

    @property
    def fallback_model(self) -> str | None:
        """後備模型（None 表示無後備）。"""
        xingcheng_caps = self._xingcheng_capabilities()
        fb = xingcheng_caps.get("fallback_model")
        return str(fb) if fb else None

    # ==================================================================
    # 2. 能力分類與模組清單 (capabilities)
    # ==================================================================

    @property
    def capability_categories(self) -> dict[str, list[str]]:
        """能力分類（understanding/research/data/engineering/general/learning/coordination）。"""
        xingcheng_caps = self._xingcheng_capabilities()
        categories = xingcheng_caps.get("capability_categories") or {}
        return {str(k): list(v) for k, v in categories.items()} if isinstance(categories, dict) else {}

    @property
    def modules(self) -> list[str]:
        """能力模組清單。"""
        xingcheng_caps = self._xingcheng_capabilities()
        return list(xingcheng_caps.get("modules") or [])

    @property
    def semantic_planning(self) -> str:
        """語意規劃策略。"""
        xingcheng_caps = self._xingcheng_capabilities()
        return str(xingcheng_caps.get("semantic_planning") or "")

    @property
    def semantic_understanding(self) -> list[str]:
        """語意理解範圍。"""
        xingcheng_caps = self._xingcheng_capabilities()
        understanding = xingcheng_caps.get("semantic_understanding") or []
        return list(understanding) if isinstance(understanding, list) else []

    @property
    def max_concurrency(self) -> int:
        """最大模型並行數。"""
        autonomous = self._autonomous_agent()
        return int(autonomous.get("maximum_model_concurrency") or 4)

    # ==================================================================
    # 3. 模型模式收斂 (model_modes) — 取代舊的扁平 model_registry
    # ==================================================================

    @property
    def main_model(self) -> dict[str, Any]:
        """主模型 — 綜合能力（協調、理解、整合、檢查、裁決）。

        收斂自 manifest 中的：
          coordination_owner / collaboration_owner / generalist_owner /
          understanding_authority / allocation_authority / integration_authority /
          inspection_authority / result_authority / model_failure_adjudicator /
          analysis_final_coordinator / ai-connections.final_coordinator
        """
        xingcheng_caps = self._xingcheng_capabilities()
        automatic_workflow = xingcheng_caps.get("automatic_workflow") or {}
        ai_connections = self._business_domain("ai-connections")
        investment_analysis = self._business_domain("investment-analysis")

        # 從 manifest 讀取實際模型 ID，fallback 到預設值
        model_id = str(
            xingcheng_caps.get("coordination_owner")
            or xingcheng_caps.get("generalist_owner")
            or automatic_workflow.get("understanding_authority")
            or _MAIN_MODEL_ID
        )

        return {
            "mode": "main",
            "model": model_id,
            "role": "comprehensive",
            "responsibilities": list(_MAIN_MODEL_RESPONSIBILITIES),
            "manifest_mapping": {
                "coordination_owner": str(xingcheng_caps.get("coordination_owner") or ""),
                "collaboration_owner": str(xingcheng_caps.get("collaboration_owner") or ""),
                "generalist_owner": str(xingcheng_caps.get("generalist_owner") or ""),
                "understanding_authority": str(automatic_workflow.get("understanding_authority") or ""),
                "allocation_authority": str(automatic_workflow.get("allocation_authority") or ""),
                "integration_authority": str(automatic_workflow.get("integration_authority") or ""),
                "inspection_authority": str(automatic_workflow.get("inspection_authority") or ""),
                "result_authority": str(automatic_workflow.get("result_authority") or ""),
                "model_failure_adjudicator": str(automatic_workflow.get("model_failure_adjudicator") or ""),
                "analysis_final_coordinator": str(investment_analysis.get("analysis_final_coordinator") or ""),
                "ai_connections_final_coordinator": str(ai_connections.get("final_coordinator") or ""),
            },
        }

    @property
    def chat_mode(self) -> dict[str, Any]:
        """聊天模式 — 對話、理解、回應、文件閱讀、視覺辨識。

        收斂自 manifest 中的：
          understanding_authority / coordination_owner（對話理解）
          document_reading.owner（文件閱讀）
          visual_file_recognition_authority.model（視覺辨識）
        """
        xingcheng_caps = self._xingcheng_capabilities()
        automatic_workflow = xingcheng_caps.get("automatic_workflow") or {}
        document_reading = xingcheng_caps.get("document_reading") or {}
        visual = automatic_workflow.get("visual_file_recognition_authority") or {}

        model_id = str(
            automatic_workflow.get("understanding_authority")
            or xingcheng_caps.get("coordination_owner")
            or _CHAT_MODE_MODEL_ID
        )

        return {
            "mode": "chat",
            "model": model_id,
            "role": "conversation-understanding-response",
            "responsibilities": list(_CHAT_MODE_RESPONSIBILITIES),
            "sub_models": {
                "document_reading": str(document_reading.get("owner") or _CHAT_MODE_DOCUMENT_READING_MODEL),
                "visual_recognition": str(visual.get("model") or _CHAT_MODE_VISUAL_MODEL),
            },
            "document_reading_config": dict(document_reading) if isinstance(document_reading, dict) else {},
            "visual_config": dict(visual) if isinstance(visual, dict) else {},
        }

    @property
    def programming_mode(self) -> dict[str, Any]:
        """編程模式 — 程式碼執行、生成、分析、重構。

        收斂自 manifest 中的：
          coding_engine_owner / execution_authority / autonomous_agent.execution
        """
        xingcheng_caps = self._xingcheng_capabilities()
        automatic_workflow = xingcheng_caps.get("automatic_workflow") or {}
        autonomous = self._autonomous_agent()

        model_id = str(
            xingcheng_caps.get("coding_engine_owner")
            or automatic_workflow.get("execution_authority")
            or _PROGRAMMING_MODE_MODEL_ID
        )

        return {
            "mode": "programming",
            "model": model_id,
            "role": "code-execution-generation-analysis",
            "responsibilities": list(_PROGRAMMING_MODE_RESPONSIBILITIES),
            "manifest_mapping": {
                "coding_engine_owner": str(xingcheng_caps.get("coding_engine_owner") or ""),
                "execution_authority": str(automatic_workflow.get("execution_authority") or ""),
                "autonomous_execution": str(autonomous.get("execution") or ""),
            },
        }

    @property
    def model_modes(self) -> dict[str, Any]:
        """三模式收斂總覽。"""
        return {
            "main_model": self.main_model,
            "chat_mode": self.chat_mode,
            "programming_mode": self.programming_mode,
        }

    # --- 向後相容：舊的扁平 model_registry ---

    @property
    def model_registry(self) -> dict[str, str]:
        """扁平模型分配表（向後相容）。

        已收斂為三模式，此屬性保留供舊程式碼過渡使用。
        """
        xingcheng_caps = self._xingcheng_capabilities()
        automatic_workflow = xingcheng_caps.get("automatic_workflow") or {}
        registry: dict[str, str] = {}
        for key in (
            "coordination_owner",
            "collaboration_owner",
            "training_owner",
            "data_owner",
            "generalist_owner",
            "investment_engine_owner",
            "mathematical_engine_owner",
            "coding_engine_owner",
        ):
            value = xingcheng_caps.get(key)
            if value:
                registry[key] = str(value)
        for key in (
            "understanding_authority",
            "allocation_authority",
            "integration_authority",
            "execution_authority",
            "inspection_authority",
            "result_authority",
            "model_failure_adjudicator",
        ):
            value = automatic_workflow.get(key)
            if value:
                registry[key] = str(value)
        return registry

    @property
    def autonomous_agent(self) -> dict[str, Any]:
        """自主代理設定。"""
        return self._autonomous_agent()

    @property
    def automatic_workflow(self) -> dict[str, Any]:
        """自動化工作流設定。"""
        xingcheng_caps = self._xingcheng_capabilities()
        return dict(xingcheng_caps.get("automatic_workflow") or {})

    @property
    def dynamic_model_dispatch(self) -> dict[str, Any]:
        """動態模型分派設定（來自 investment-analysis 能力面）。"""
        investment_analysis = self._business_domain("investment-analysis")
        dispatch = investment_analysis.get("dynamic_model_dispatch") or {}
        return dict(dispatch) if isinstance(dispatch, dict) else {}

    # ==================================================================
    # 4. 業務能力面 (business_domains)
    # ==================================================================

    def business_domain(self, domain_id: str) -> dict[str, Any]:
        """取得指定業務能力面的設定。"""
        return self._business_domain(domain_id)

    @property
    def business_domains(self) -> dict[str, dict[str, Any]]:
        """所有業務能力面的完整快照。"""
        manifest = self._manifest()
        capabilities = manifest.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            return {}
        return {
            domain_id: dict(config) if isinstance(config, dict) else {}
            for domain_id, config in capabilities.items()
        }

    @property
    def business_domain_ids(self) -> list[str]:
        """所有業務能力面 ID 清單。"""
        return sorted(self.business_domains.keys())

    # --- 投資市場搜尋 ---

    @property
    def investment_market_search(self) -> dict[str, Any]:
        """投資市場搜尋能力面。"""
        return self._business_domain("investment-market-search")

    # --- 投資分析 ---

    @property
    def investment_analysis(self) -> dict[str, Any]:
        """投資分析能力面。"""
        return self._business_domain("investment-analysis")

    # --- 程式升級優化 ---

    @property
    def upgrade_optimization(self) -> dict[str, Any]:
        """程式升級優化能力面。"""
        return self._business_domain("upgrade-optimization")

    # --- 專案程式 ---

    @property
    def star_project_programming(self) -> dict[str, Any]:
        """專案程式能力面。"""
        return self._business_domain("star-project-programming")

    # --- AI 連線 ---

    @property
    def ai_connections(self) -> dict[str, Any]:
        """AI 連線能力面。"""
        return self._business_domain("ai-connections")

    # --- 文件閱讀 ---

    @property
    def document_reading(self) -> dict[str, Any]:
        """文件閱讀能力。"""
        xingcheng_caps = self._xingcheng_capabilities()
        doc_reading = xingcheng_caps.get("document_reading") or {}
        return dict(doc_reading) if isinstance(doc_reading, dict) else {}

    # ==================================================================
    # 完整能力快照
    # ==================================================================

    def capability_status(self) -> dict[str, Any]:
        """原生模型能力的完整快照（收攏所有能力面 + 三模式收斂）。"""
        return {
            # 1. 核心
            "core": {
                "kind": self.kind,
                "module_id": self.module_id,
                "native_model_authority": self.native_model_authority,
                "model_permissions": self.model_permissions,
                "empowered_powers": list(self.empowered_powers),
                "prohibited_powers": list(self.prohibited_powers),
                "fixed_responsibilities": self.fixed_responsibilities,
                "entry_ownership": self.entry_ownership,
                "entry_sources": self.entry_sources,
                "specialist_direct_access": self.specialist_direct_access,
                "fallback_model": self.fallback_model,
            },
            # 2. 能力分類
            "capabilities": {
                "capability_categories": self.capability_categories,
                "modules": self.modules,
                "semantic_planning": self.semantic_planning,
                "semantic_understanding": self.semantic_understanding,
                "max_concurrency": self.max_concurrency,
            },
            # 3. 模型模式收斂（核心重組）
            "model_modes": self.model_modes,
            # 向後相容
            "model_registry": self.model_registry,
            "autonomous_agent": self.autonomous_agent,
            "automatic_workflow": self.automatic_workflow,
            "dynamic_model_dispatch": self.dynamic_model_dispatch,
            # 4. 業務能力面
            "business_domains": {
                "ids": self.business_domain_ids,
                "investment_market_search": self.investment_market_search,
                "investment_analysis": self.investment_analysis,
                "upgrade_optimization": self.upgrade_optimization,
                "star_project_programming": self.star_project_programming,
                "ai_connections": self.ai_connections,
                "document_reading": self.document_reading,
            },
            # 共同邊界
            "execution": False,
            "delegation": "governed-executor-only",
        }

    # ==================================================================
    # Internals — manifest 載入與子結構存取
    # ==================================================================

    def _manifest(self) -> dict[str, Any]:
        """載入 local-model/manifest.json（快取）。"""
        if self._manifest_cache is not None:
            return self._manifest_cache
        project_root = Path(getattr(self.app, "project_root", Path.cwd()))
        manifest_path = project_root / "Standalone tools" / "local-model" / "manifest.json"
        try:
            self._manifest_cache = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            self._manifest_cache = {}
        return self._manifest_cache

    def _capabilities(self) -> dict[str, Any]:
        """manifest.capabilities 區塊。"""
        manifest = self._manifest()
        caps = manifest.get("capabilities") or {}
        return caps if isinstance(caps, dict) else {}

    def _xingcheng_capabilities(self) -> dict[str, Any]:
        """manifest.capabilities.xingcheng 區塊（核心能力設定所在）。"""
        caps = self._capabilities()
        xingcheng = caps.get("xingcheng") or {}
        return xingcheng if isinstance(xingcheng, dict) else {}

    def _autonomous_agent(self) -> dict[str, Any]:
        """manifest.capabilities.xingcheng.autonomous_agent 區塊。"""
        xingcheng_caps = self._xingcheng_capabilities()
        agent = xingcheng_caps.get("autonomous_agent") or {}
        return agent if isinstance(agent, dict) else {}

    def _business_domain(self, domain_id: str) -> dict[str, Any]:
        """manifest.capabilities[domain_id] 區塊。"""
        caps = self._capabilities()
        domain = caps.get(domain_id) or {}
        return domain if isinstance(domain, dict) else {}


__all__ = [
    "XINGCHENG_EMPOWERED_POWERS",
    "XINGCHENG_KIND",
    "XINGCHENG_PROHIBITED_POWERS",
    "XingchengNativeModel",
]
