from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Final

from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)


AI_TASK_SCHEMA_VERSION: Final[str] = "1.0"
AI_TASK_PROVIDERS: Final[frozenset[str]] = frozenset(
    {
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "perplexity",
        "google-search",
    }
)
AI_TASK_SCOPES: Final[frozenset[str]] = frozenset({"general", "investment"})
AI_TASK_TYPES: Final[frozenset[str]] = frozenset(
    {
        "general",
        "orchestration",
        "search",
        "advanced_search",
        "calculation",
        "longform",
        "reasoning",
        "social_media",
        "trends",
        "breaking_news",
    }
)
AI_TASK_REQUESTERS: Final[frozenset[str]] = frozenset(
    {"local-ai", "ai-collaboration"}
)
MAX_TASK_CONTENT_CHARACTERS: Final[int] = 64_000
MAX_MEMORY_ITEMS: Final[int] = 20
MAX_MEMORY_CONTENT_CHARACTERS: Final[int] = 2_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_text(value: Any, *, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise permission_denied()
    normalized = value.strip()
    if (required and not normalized) or len(normalized) > maximum:
        raise permission_denied()
    return normalized


def sanitize_memory_context(raw_items: Any) -> list[dict[str, Any]]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise permission_denied()
    sanitized: list[dict[str, Any]] = []
    for raw in raw_items[:MAX_MEMORY_ITEMS]:
        if not isinstance(raw, dict):
            raise permission_denied()
        content = _normalized_text(
            raw.get("content", ""),
            maximum=MAX_MEMORY_CONTENT_CHARACTERS,
        )
        sanitized.append(
            {
                "memory_id": _normalized_text(
                    str(raw.get("memory_id") or uuid.uuid4().hex[:16]),
                    maximum=64,
                ),
                "kind": _normalized_text(
                    str(raw.get("kind") or "context"), maximum=64
                ),
                "title": _normalized_text(
                    str(raw.get("title") or content[:80]), maximum=160
                ),
                "content": content,
                "origin_model_id": _normalized_text(
                    str(raw.get("origin_model_id") or "star-main-native-model"),
                    maximum=96,
                ),
                "business_scope": _normalized_text(
                    str(raw.get("business_scope") or "general"), maximum=32
                ),
            }
        )
    return sanitized


def build_ai_task_envelope(
    *,
    provider: str,
    business_scope: str,
    task_type: str,
    content: str,
    requested_by: str,
    memory_context: Any = None,
    memory_writeback: bool = True,
) -> dict[str, Any]:
    normalized_provider = str(provider or "").strip().casefold()
    normalized_scope = str(business_scope or "").strip().casefold()
    normalized_task = str(task_type or "").strip().casefold()
    normalized_requester = str(requested_by or "").strip().casefold()
    if (
        normalized_provider not in AI_TASK_PROVIDERS
        or normalized_scope not in AI_TASK_SCOPES
        or normalized_task not in AI_TASK_TYPES
        or normalized_requester not in AI_TASK_REQUESTERS
    ):
        raise permission_denied()
    normalized_content = _normalized_text(
        content, maximum=MAX_TASK_CONTENT_CHARACTERS
    )
    return {
        "schema_version": AI_TASK_SCHEMA_VERSION,
        "task_id": uuid.uuid4().hex,
        "provider": normalized_provider,
        "business_scope": normalized_scope,
        "task_type": normalized_task,
        "content": normalized_content,
        "requested_by": normalized_requester,
        "response_recipient": normalized_requester,
        "memory_context": sanitize_memory_context(memory_context),
        "memory_policy": {
            "broker": "star-main-native-model",
            "direct_database_access": False,
            "writeback": "candidate-only" if memory_writeback else "disabled",
            "model_database_isolation": "mandatory",
        },
        "created_at": utc_now(),
    }


def build_memory_candidate(
    task: dict[str, Any], *, source_agent_id: str, content: str
) -> dict[str, Any]:
    normalized = _normalized_text(
        str(content or ""), maximum=MAX_TASK_CONTENT_CHARACTERS
    )
    clipped = normalized[:4_000]
    digest = hashlib.sha256(
        json.dumps(
            {
                "provider": task.get("provider"),
                "scope": task.get("business_scope"),
                "task": task.get("task_type"),
                "content": clipped,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "candidate_id": digest[:24],
        "kind": str(task.get("task_type") or "external-ai"),
        "title": clipped.splitlines()[0][:120],
        "content": clipped,
        "business_scope": str(task.get("business_scope") or "general"),
        "source_agent_id": _normalized_text(source_agent_id, maximum=64),
        "source_provider": str(task.get("provider") or ""),
        "status": "candidate",
        "direct_database_write": False,
        "reviewer": "star-main-native-model",
        "created_at": utc_now(),
    }
