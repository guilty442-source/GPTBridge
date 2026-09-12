from __future__ import annotations

import json
import sqlite3
from typing import Any

from .collab_repo_constants import DEFAULT_AGENTS, utc_now


class CollabRepoSchemaMixin:
    """Schema creation and default-agent seeding for AiCollaborationRepository."""

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_nexus_agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    home_url TEXT NOT NULL,
                    general_url TEXT NOT NULL DEFAULT '',
                    investment_url TEXT NOT NULL DEFAULT '',
                    star_training_url TEXT NOT NULL DEFAULT '',
                    general_enabled INTEGER NOT NULL DEFAULT 1,
                    investment_enabled INTEGER NOT NULL DEFAULT 1,
                    business_capabilities_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    selected INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_group_messages (
                    message_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    selected_agents_json TEXT NOT NULL DEFAULT '[]',
                    business_scope TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_nexus_group_messages_created
                ON ai_nexus_group_messages(created_at DESC);

                CREATE TABLE IF NOT EXISTS ai_nexus_agent_responses (
                    response_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    execution_provider TEXT NOT NULL DEFAULT '',
                    transport TEXT NOT NULL DEFAULT '',
                    fallback_json TEXT NOT NULL DEFAULT '{}',
                    memory_candidates_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES ai_nexus_group_messages(message_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ai_nexus_agent_responses_message
                ON ai_nexus_agent_responses(message_id, agent_id);

                CREATE TABLE IF NOT EXISTS ai_nexus_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    participant_agents_json TEXT NOT NULL DEFAULT '[]',
                    conclusion TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_memory_items (
                    memory_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    business_scope TEXT NOT NULL DEFAULT 'general',
                    source_agent_id TEXT NOT NULL DEFAULT '',
                    owner_model_id TEXT NOT NULL DEFAULT 'ai-collaboration',
                    status TEXT NOT NULL DEFAULT 'accepted',
                    content_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_knowledge_files (
                    file_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    file_type TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    content_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_tools (
                    tool_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(connection, "ai_nexus_agents", "general_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_agents", "investment_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_agents", "star_training_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_agents", "general_enabled", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "ai_nexus_agents", "investment_enabled", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "ai_nexus_agents", "business_capabilities_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "ai_nexus_group_messages", "business_scope", "TEXT NOT NULL DEFAULT 'general'")
            self._ensure_column(connection, "ai_nexus_agent_responses", "error_code", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_agent_responses", "execution_provider", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_agent_responses", "transport", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_agent_responses", "fallback_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(connection, "ai_nexus_agent_responses", "memory_candidates_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "ai_nexus_memory_items", "business_scope", "TEXT NOT NULL DEFAULT 'general'")
            self._ensure_column(connection, "ai_nexus_memory_items", "source_agent_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "ai_nexus_memory_items", "owner_model_id", "TEXT NOT NULL DEFAULT 'ai-collaboration'")
            self._ensure_column(connection, "ai_nexus_memory_items", "status", "TEXT NOT NULL DEFAULT 'accepted'")
            self._ensure_column(connection, "ai_nexus_memory_items", "content_hash", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "UPDATE ai_nexus_agents SET general_url = home_url WHERE TRIM(general_url) = ''"
            )
            connection.execute(
                """
                UPDATE ai_nexus_agents
                SET star_training_url = general_url
                WHERE agent_id = 'chatgpt' AND TRIM(star_training_url) = ''
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _ensure_default_agents(self) -> None:
        now = utc_now()
        with self._connect() as connection:
            for agent in DEFAULT_AGENTS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ai_nexus_agents
                    (agent_id, name, provider, home_url, general_url, investment_url, star_training_url,
                     general_enabled, investment_enabled, business_capabilities_json,
                     enabled, selected, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, 1, ?, 'idle', ?)
                    """,
                    (
                        agent["agent_id"],
                        agent["name"],
                        agent["provider"],
                        agent["home_url"],
                        agent["home_url"],
                        agent["home_url"],
                        agent["home_url"] if agent["agent_id"] == "chatgpt" else "",
                        json.dumps(agent.get("business_capabilities", []), ensure_ascii=False),
                        1 if agent.get("selected", True) else 0,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE ai_nexus_agents
                    SET investment_url = ?, updated_at = ?
                    WHERE agent_id = ? AND TRIM(investment_url) = ''
                    """,
                    (agent["home_url"], now, agent["agent_id"]),
                )
                connection.execute(
                    """
                    UPDATE ai_nexus_agents
                    SET business_capabilities_json = ?, updated_at = ?
                    WHERE agent_id = ? AND business_capabilities_json IN ('', '[]')
                    """,
                    (
                        json.dumps(agent.get("business_capabilities", []), ensure_ascii=False),
                        now,
                        agent["agent_id"],
                    ),
                )
            rows = connection.execute(
                "SELECT agent_id, business_capabilities_json FROM ai_nexus_agents"
            ).fetchall()
            for row in rows:
                agent_id = str(row["agent_id"])
                try:
                    capabilities = [
                        str(item)
                        for item in json.loads(
                            str(row["business_capabilities_json"] or "[]")
                        )
                    ]
                except (TypeError, json.JSONDecodeError):
                    capabilities = []
                if agent_id == "chatgpt":
                    capabilities = list(
                        dict.fromkeys([*capabilities, "comprehensive", "orchestration"])
                    )
                else:
                    capabilities = [
                        item
                        for item in capabilities
                        if item not in {"comprehensive", "orchestration"}
                    ]
                connection.execute(
                    "UPDATE ai_nexus_agents SET business_capabilities_json = ? WHERE agent_id = ?",
                    (json.dumps(capabilities, ensure_ascii=False), agent_id),
                )
