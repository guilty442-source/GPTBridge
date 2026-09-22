"""Shared test support (split from consolidated test_ai_collaboration.py)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

import asyncio
import sys
from pathlib import Path

import pytest


TOOL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TOOL_ROOT.parent
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
SHARED_SRC = PROJECT_ROOT / "shared-layer" / "src"
for path in (SERVICES_ROOT, SHARED_SRC, PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ai_collaboration.infrastructure.repository import AiCollaborationRepository
from ai_collaboration.integration.provider_session import AiCollaborationProviderSession
from ai_collaboration.application.service import AiCollaborationService



class _FakeBrowserSession:
    def __init__(self) -> None:
        self.opened_agent: dict[str, object] | None = None
        self.sent_providers: list[str] = []
        self.sent_agents: list[dict[str, object]] = []

    async def open_agent(self, agent: dict[str, object]) -> dict[str, object]:
        self.opened_agent = dict(agent)
        return {"ok": True, "url": agent["home_url"]}

    async def send_prompt(
        self, agent: dict[str, object], prompt: str
    ) -> dict[str, str]:
        self.sent_agents.append(dict(agent))
        self.sent_providers.append(str(agent["provider"]))
        if agent["provider"] == "gemini":
            assert "Gemini 支援的 Google" in prompt
            return {"status": "completed", "content": "Gemini 已整理", "error": ""}
        assert agent["provider"] == "chatgpt"
        assert "最終統籌" in prompt
        return {"status": "completed", "content": "ChatGPT 最終統籌", "error": ""}

    async def close_background_context(self) -> None:
        return None


class _FakeProviderSession:
    def __init__(self) -> None:
        self.tasks: list[dict[str, object]] = []
        self.providers: list[str] = []

    async def send_task(
        self, agent: dict[str, object], task: dict[str, object]
    ) -> dict[str, object]:
        self.tasks.append(dict(task))
        self.providers.append(str(agent["provider"]))
        return {
            "status": "completed",
            "provider": "chatgpt",
            "content": "已整合",
            "transport": "embedded-browser-view",
            "fallback": {
                "used": True,
                "requested_provider": agent["provider"],
                "effective_provider": "chatgpt",
            },
            "memory_candidates": [
                {
                    "candidate_id": "candidate-1",
                    "kind": "longform",
                    "title": "摘要",
                    "content": "已整合",
                    "status": "candidate",
                    "source_agent_id": agent["agent_id"],
                    "direct_database_write": False,
                }
            ],
        }

    async def close_background_context(self) -> None:
        return None


__all__ = [n for n in dir() if not n.startswith("__")]
