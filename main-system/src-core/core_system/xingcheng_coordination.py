"""xingcheng_coordination — 星澄輔助系統的智慧管理權.

星澄（Xingcheng）是 GPTBridge 的原生輔助系統，與系統主宰同級（peer）。
此模組定義星澄的智慧管理權面：協調、觀察、建議、解釋。

星澄的職責已拆分為三個獨立面向：
  * xingcheng_personality.py  — 人格設定（身份、名稱、展示屬性）
  * xingcheng_native_model.py — 原生模型能力（模型權限、能力分類）
  * xingcheng_coordination.py — 輔助系統智慧管理權（此模組）

智慧管理權面：
  * mode              = intelligent-management（智慧管理）
  * authority         = read-only-management（唯讀管理，無執行權）
  * execution         = false（不執行）
  * git_write         = false
  * sql_write         = false
  * rag_mutation      = false
  * decision & thinking = referenced from the Governance Codex

此模組為輕量協調層（不匯入重型 AI/模型堆疊），從母應用的 governed-tool
狀態讀取星澄的駐留狀態，母進程永不在進程內執行星澄的執行堆疊。
"""

from __future__ import annotations

from typing import Any

from .codex_decision import decision_basis
from .versioning import application_version
from .xingcheng_native_model import (
    XINGCHENG_EMPOWERED_POWERS,
    XINGCHENG_KIND,
    XINGCHENG_PROHIBITED_POWERS,
)
from .xingcheng_personality import (
    XINGCHENG_MODULE_ID,
    XINGCHENG_RANK,
    XINGCHENG_ROLE,
)

XINGCHENG_MODE = "intelligent-management"


class XingchengCoordination:
    """星澄輔助系統的智慧管理權 — 決策層級協調。

    每個決策和思考都引用 Governance Codex（areas ``xingcheng`` /
    ``xingcheng-thinking``）；此代理不擁有自己的決策來源。星澄不執行。

    人格設定和原生模型能力透過 ``personality`` 和 ``native_model``
    屬性暴露，由各自的模組管理。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        # 延遲匯入以避免循環依賴
        from .xingcheng_personality import XingchengPersonality
        from .xingcheng_native_model import XingchengNativeModel
        self._personality = XingchengPersonality(app)
        self._native_model = XingchengNativeModel(app)

    @property
    def personality(self) -> Any:
        """星澄人格設定（身份、名稱、展示屬性）。"""
        return self._personality

    @property
    def native_model(self) -> Any:
        """星澄原生模型能力（模型權限、能力分類）。"""
        return self._native_model

    # ------------------------------------------------------------------
    # 智慧管理權面
    # ------------------------------------------------------------------

    def coordination_status(self) -> dict[str, Any]:
        """星澄輔助系統智慧管理權的完整快照。"""

        app_version = application_version(self.app.project_root)
        delegated, tool_state = self._xingcheng_tool_state()

        return {
            "module_id": XINGCHENG_MODULE_ID,
            "role": XINGCHENG_ROLE,
            "rank": XINGCHENG_RANK,
            "kind": XINGCHENG_KIND,
            "mode": XINGCHENG_MODE,
            "authority": {
                "native_model": True,
                "decision_and_orchestration": True,
                "read_only_management": True,
                "execution": False,
                "git_write": False,
                "sql_write": False,
                "rag_mutation": False,
            },
            "powers": {
                "empowered": list(XINGCHENG_EMPOWERED_POWERS),
                "prohibited": list(XINGCHENG_PROHIBITED_POWERS),
            },
            "version": app_version,
            "delegated": delegated,
            "tool_state": tool_state,
            "delegation": "governed-executor-only",
            "personality": self._personality.personality_status(),
            "native_model": self._native_model.capability_status(),
            "thinking": decision_basis("xingcheng-thinking"),
            "decision": decision_basis("xingcheng"),
        }

    def orchestration_status(self) -> dict[str, Any]:
        """統一子系統視圖（供主宰的協調報告使用）。"""

        delegated, tool_state = self._xingcheng_tool_state()
        resident = bool(tool_state.get("resident_service")) if tool_state else False
        state = "delegated" if delegated else ("resident" if resident else "idle")
        return {
            "name": "xingcheng",
            "module_id": XINGCHENG_MODULE_ID,
            "role": XINGCHENG_ROLE,
            "rank": XINGCHENG_RANK,
            "kind": XINGCHENG_KIND,
            "state": state,
            "delegation": "governed-executor-only",
            "authority": "intelligent-management",
            "execution": False,
            "powers": {
                "empowered": list(XINGCHENG_EMPOWERED_POWERS),
                "prohibited": list(XINGCHENG_PROHIBITED_POWERS),
            },
            "personality": self._personality.personality_status(),
            "native_model": self._native_model.capability_status(),
            "thinking": decision_basis("xingcheng-thinking"),
            "decision": decision_basis("xingcheng"),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _xingcheng_tool_state(self) -> tuple[bool, dict[str, Any] | None]:
        """從母應用的狀態定位星澄的駐留狀態。

        回傳 ``(delegated, tool_state)``；``delegated`` 為 True 時表示
        governed tool 已移交給執行器。
        """

        toolbox = getattr(self.app, "toolbox_service", None)
        try:
            status = toolbox.status() if hasattr(toolbox, "status") else {}
        except Exception:
            status = {}

        tools = status.get("tools") or {}
        tool = None
        if isinstance(tools, dict):
            tool = tools.get(XINGCHENG_MODULE_ID)
        elif isinstance(tools, list):
            for item in tools:
                if isinstance(item, dict) and item.get("tool_id") == XINGCHENG_MODULE_ID:
                    tool = item
                    break

        if not isinstance(tool, dict):
            # 回退到預設工具啟動記錄
            fallback = getattr(self.app, "default_tool_startup", {})
            record = fallback.get(XINGCHENG_MODULE_ID)
            if isinstance(record, dict):
                delegated = bool(record.get("ok") is True)
                return delegated, {"resident_service": delegated, "startup": record}
            return False, None

        delegated = bool(
            tool.get("delegated") or tool.get("executor_state") or tool.get("ok")
        )
        return delegated, tool


__all__ = [
    "XINGCHENG_MODE",
    "XINGCHENG_MODULE_ID",
    "XINGCHENG_ROLE",
    "XingchengCoordination",
]
