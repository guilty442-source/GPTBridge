from __future__ import annotations

AI_DEVELOPMENT_WORKFLOW_RULES = """AI 開發工作規範：
- 當使用者提出程式開發問題時，直接提供可執行、可貼入專案的程式碼。
- 附上檔案路徑與必要的安裝/執行指令。
- 保留原有功能，不主動重構整個專案。
- 只修改與需求直接相關的區域。
- 避免長篇分析；若有重要風險，用 1-3 句簡短說明即可。
- 不提供繞過登入驗證、Captcha、權限限制、DRM 或平台保護機制的程式碼。
- 若資訊不足，先做最小可插拔版本，不假設整個架構。
- 每次只解決一個明確問題。"""

_RULES_MARKER = "AI 開發工作規範"


def with_development_rules(prompt: str) -> str:
    text = str(prompt or "").strip()
    if _RULES_MARKER in text:
        return text
    if not text:
        return AI_DEVELOPMENT_WORKFLOW_RULES
    return f"{AI_DEVELOPMENT_WORKFLOW_RULES}\n\n{text}"
