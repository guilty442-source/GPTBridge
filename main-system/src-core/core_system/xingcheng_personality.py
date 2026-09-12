"""xingcheng_personality — 星澄人格設定.

星澄（Xingcheng）是 GPTBridge 的原生輔助系統人格。此模組定義星澄的
身份、名稱、展示名等人格屬性，與原生模型能力和智慧管理權分離。

人格設定來源：
  * Governance Codex — 星澄的主宰身份與權力邊界
  * local-model/manifest.json — 星澄的展示名稱與身份標籤

此模組為唯讀協調層，不執行任何 AI/模型推理。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX


_XINGCHENG_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "xingcheng"),
    None,
)
if _XINGCHENG_SOVEREIGN is None:
    raise RuntimeError("xingcheng sovereign not found in Governance Codex")

# 人格身份常量 — 來自 Governance Codex
XINGCHENG_IDENTITY = "星澄"
XINGCHENG_ROLE = _XINGCHENG_SOVEREIGN.id
XINGCHENG_MODULE_ID = _XINGCHENG_SOVEREIGN.id
XINGCHENG_RANK = _XINGCHENG_SOVEREIGN.rank

# 預設展示名稱（可被 manifest 覆蓋）
_DEFAULT_NATIVE_MODEL_DISPLAY_NAME = "星澄原生模型"
_DEFAULT_TOOL_DISPLAY_NAME = "本地模型"


class XingchengPersonality:
    """星澄人格設定 — 身份、名稱、展示屬性。

    從 manifest 載入人格展示屬性，從 Codex 載入身份與權力邊界。
    此類別為唯讀快照，不持有可變狀態。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._manifest_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # 身份屬性
    # ------------------------------------------------------------------

    @property
    def identity(self) -> str:
        """星澄的身份名稱（人格核心）。"""
        return XINGCHENG_IDENTITY

    @property
    def role(self) -> str:
        """星澄在 Governance Codex 中的主宰角色 ID。"""
        return XINGCHENG_ROLE

    @property
    def rank(self) -> str:
        """星澄的層級（與系統主宰同級的輔助系統）。"""
        return XINGCHENG_RANK

    @property
    def module_id(self) -> str:
        """星澄的模組 ID。"""
        return XINGCHENG_MODULE_ID

    # ------------------------------------------------------------------
    # 展示屬性（從 manifest 載入）
    # ------------------------------------------------------------------

    @property
    def native_model_display_name(self) -> str:
        """原生模型的展示名稱。"""
        manifest = self._manifest()
        return str(
            manifest.get("native_model_display_name")
            or _DEFAULT_NATIVE_MODEL_DISPLAY_NAME
        )

    @property
    def tool_display_name(self) -> str:
        """工具的展示名稱。"""
        manifest = self._manifest()
        return str(
            manifest.get("tool_display_name")
            or _DEFAULT_TOOL_DISPLAY_NAME
        )

    @property
    def assistant_identity(self) -> str:
        """助理身份標籤。"""
        manifest = self._manifest()
        return str(manifest.get("assistant_identity") or XINGCHENG_IDENTITY)

    @property
    def operation_mode(self) -> str:
        """操作模式。"""
        manifest = self._manifest()
        return str(manifest.get("operation_mode") or "context-aware-multitask-model-platform")

    # ------------------------------------------------------------------
    # 人格快照
    # ------------------------------------------------------------------

    def personality_status(self) -> dict[str, Any]:
        """人格設定的完整快照。"""
        return {
            "identity": self.identity,
            "role": self.role,
            "rank": self.rank,
            "module_id": self.module_id,
            "native_model_display_name": self.native_model_display_name,
            "tool_display_name": self.tool_display_name,
            "assistant_identity": self.assistant_identity,
            "operation_mode": self.operation_mode,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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


__all__ = [
    "XINGCHENG_IDENTITY",
    "XINGCHENG_MODULE_ID",
    "XINGCHENG_RANK",
    "XINGCHENG_ROLE",
    "XingchengPersonality",
]
