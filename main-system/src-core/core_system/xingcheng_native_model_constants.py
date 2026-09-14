"""xingcheng_native_model_constants — 三模式收斂常數與預設權限.

從 xingcheng_native_model.py 提取的模組級常數、預設權限、
三模式模型 ID 與職責定義。
"""

from __future__ import annotations

from governance_rule.execution.codex_official import official_self_declaration

from .xingcheng_personality import XINGCHENG_MODULE_ID

# A74/A174: 星澄 self-declaration through the official entry single-use
# session — resolved by sovereign identity, not an area scan.
_XINGCHENG_SOVEREIGN = official_self_declaration(XINGCHENG_MODULE_ID)
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
