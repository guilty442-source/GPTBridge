from __future__ import annotations

from pathlib import Path
from typing import Any

from .browser_session import AiNexusBrowserSession


AGENTS: dict[str, dict[str, str]] = {
    "gemini": {
        "name": "Gemini",
        "url": "https://gemini.google.com/",
        "role": "主要 AI：報價、搜尋、來源與時間戳",
    },
    "chatgpt": {
        "name": "ChatGPT",
        "url": "https://chatgpt.com/",
        "role": "主要 AI：特徵萃取、過濾雜訊、標準條列化",
    },
    "claude": {
        "name": "Claude",
        "url": "https://claude.ai/",
        "role": "主要 AI：財報長文閱讀、風險拆解、投資論點反證",
    },
    "perplexity": {
        "name": "Perplexity",
        "url": "https://www.perplexity.ai/",
        "role": "主要 AI：來源搜尋、即時資料交叉驗證、引用整理",
    },
    "grok": {
        "name": "Grok",
        "url": "https://grok.com/",
        "role": "主要 AI：市場脈動、社群訊號、事件熱度追蹤",
    },
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://chat.deepseek.com/",
        "role": "主要 AI：財務邏輯、估值假設、數據推理",
    },
    "copilot": {
        "name": "Copilot",
        "url": "https://copilot.microsoft.com/",
        "role": "主要 AI：Edge/Bing 搜尋、新聞摘要、來源比對",
    },
}


class InvestmentBrowserSession:
    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root.resolve()
        self._owns_session = session is None
        self.session = session or AiNexusBrowserSession(
            profile_name="ai-assistant",
            profile_root=self.project_root / "runtime" / "browser-profiles",
        )

    @property
    def shared_profile_dir(self) -> Path:
        return Path(getattr(self.session, "shared_profile_dir", ""))

    async def open_agent(self, agent_id: str) -> dict[str, Any]:
        result = await self.session.open_agent(self._agent_payload(agent_id))
        return {"agent_id": agent_id, **result}

    async def send_prompt(self, agent_id: str, prompt: str) -> dict[str, Any]:
        return await self.session.send_prompt(self._agent_payload(agent_id), prompt)

    async def shutdown(self) -> None:
        if self._owns_session and hasattr(self.session, "shutdown"):
            await self.session.shutdown()

    @staticmethod
    def _agent_payload(agent_id: str) -> dict[str, str]:
        if agent_id not in AGENTS:
            raise ValueError(f"unknown agent: {agent_id}")
        agent = AGENTS[agent_id]
        return {
            "agent_id": agent_id,
            "name": agent["name"],
            "home_url": agent["url"],
            "role": agent["role"],
        }
