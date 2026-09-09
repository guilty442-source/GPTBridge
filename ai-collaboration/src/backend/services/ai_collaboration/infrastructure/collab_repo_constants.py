from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "agent_id": "chatgpt",
        "name": "ChatGPT",
        "provider": "chatgpt",
        "home_url": "https://chatgpt.com/",
        "business_capabilities": ["general", "comprehensive", "orchestration"],
    },
    {
        "agent_id": "claude",
        "name": "Claude",
        "provider": "claude",
        "home_url": "https://claude.ai/",
        "business_capabilities": ["general", "longform"],
    },
    {
        "agent_id": "gemini",
        "name": "Gemini",
        "provider": "gemini",
        "home_url": "https://gemini.google.com/",
        "business_capabilities": ["general", "search"],
    },
    {
        "agent_id": "grok",
        "name": "Grok",
        "provider": "grok",
        "home_url": "https://grok.com/",
        "business_capabilities": [
            "general",
            "social_media",
            "trends",
            "breaking_news",
        ],
    },
    {
        "agent_id": "deepseek",
        "name": "DeepSeek",
        "provider": "deepseek",
        "home_url": "https://chat.deepseek.com/",
        "business_capabilities": ["general", "reasoning"],
    },
    {
        "agent_id": "perplexity",
        "name": "Perplexity",
        "provider": "perplexity",
        "home_url": "https://www.perplexity.ai/",
        "business_capabilities": ["general", "advanced_search", "calculation"],
    },
    {
        "agent_id": "google-search",
        "name": "Google 搜尋",
        "provider": "google-search",
        "home_url": "https://www.google.com/",
        "business_capabilities": ["google_retrieval"],
        "selected": False,
    },
)
