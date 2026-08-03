from __future__ import annotations

import json
import sqlite3
import uuid
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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


class AiCollaborationRepository:
    def __init__(self, project_root: Path) -> None:
        self.db_path = project_root / "runtime" / "state" / "ai_collaboration.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._ensure_default_agents()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

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

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_nexus_agents ORDER BY rowid"
            ).fetchall()
        return [self._agent_row(row) for row in rows]

    def get_agents(self, agent_ids: list[str]) -> list[dict[str, Any]]:
        if not agent_ids:
            return []
        placeholders = ",".join("?" for _ in agent_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ai_nexus_agents WHERE agent_id IN ({placeholders}) ORDER BY rowid",
                agent_ids,
            ).fetchall()
        found = {str(row["agent_id"]): self._agent_row(row) for row in rows}
        return [found[agent_id] for agent_id in agent_ids if agent_id in found]

    def save_agent_selection(self, agent_ids: list[str]) -> None:
        selected = set(agent_ids)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("UPDATE ai_nexus_agents SET selected = 0, updated_at = ?", (now,))
            for agent_id in selected:
                connection.execute(
                    "UPDATE ai_nexus_agents SET selected = 1, updated_at = ? WHERE agent_id = ?",
                    (now, agent_id),
                )

    @staticmethod
    def _validated_external_url(value: str) -> str:
        normalized = str(value or "").strip()
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("URL 必須是未含帳密的 https:// 外部網址")
        hostname = parsed.hostname.casefold()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("URL 不可指向本機位址")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("URL 不可指向私人或保留網路位址")
        return normalized

    def save_agent_business_settings(
        self,
        agent_id: str,
        *,
        general_url: str,
        investment_url: str,
        general_enabled: bool,
        investment_enabled: bool,
        star_training_url: str | None = None,
        business_capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_general = self._validated_external_url(general_url)
        normalized_investment = self._validated_external_url(investment_url)
        existing_agents = self.get_agents([agent_id])
        if not existing_agents:
            raise ValueError("找不到指定的 AI")
        existing = existing_agents[0]
        normalized_star_training = ""
        if agent_id == "chatgpt":
            normalized_star_training = self._validated_external_url(
                star_training_url
                if star_training_url is not None
                else str(existing.get("star_training_url") or normalized_general)
            )
        allowed_capabilities = {
            "general",
            "comprehensive",
            "orchestration",
            "search",
            "advanced_search",
            "calculation",
            "longform",
            "reasoning",
            "social_media",
            "trends",
            "breaking_news",
            "google_retrieval",
        }
        capabilities = list(
            dict.fromkeys(
                str(item or "").strip().casefold()
                for item in (business_capabilities or [])
                if str(item or "").strip().casefold() in allowed_capabilities
            )
        )
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
        if not capabilities:
            raise ValueError("至少需要一個有效的業務能力")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE ai_nexus_agents
                SET home_url = ?, general_url = ?, investment_url = ?, star_training_url = ?,
                    general_enabled = ?, investment_enabled = ?, updated_at = ?
                    , business_capabilities_json = ?
                WHERE agent_id = ?
                """,
                (
                    normalized_general,
                    normalized_general,
                    normalized_investment,
                    normalized_star_training,
                    1 if general_enabled else 0,
                    1 if investment_enabled else 0,
                    utc_now(),
                    json.dumps(capabilities, ensure_ascii=False),
                    agent_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("找不到指定的 AI")
        return self.get_agents([agent_id])[0]

    @staticmethod
    def _agent_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            parsed = json.loads(str(item.pop("business_capabilities_json", "[]")))
        except (TypeError, ValueError):
            parsed = []
        item["business_capabilities"] = (
            [str(value) for value in parsed] if isinstance(parsed, list) else []
        )
        return item

    def update_agent_status(self, agent_id: str, status: str, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_nexus_agents
                SET status = ?, last_error = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (status, error, utc_now(), agent_id),
            )

    def create_group_message(
        self,
        content: str,
        selected_agents: list[str],
        business_scope: str = "general",
    ) -> dict[str, Any]:
        message_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_group_messages
                (message_id, role, content, selected_agents_json, business_scope, created_at)
                VALUES (?, 'user', ?, ?, ?, ?)
                """,
                (
                    message_id,
                    content,
                    json.dumps(selected_agents, ensure_ascii=False),
                    business_scope,
                    now,
                ),
            )
            for agent_id in selected_agents:
                connection.execute(
                    """
                    INSERT INTO ai_nexus_agent_responses
                    (response_id, message_id, agent_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (uuid.uuid4().hex[:16], message_id, agent_id, now, now),
                )
        return self.get_message(message_id) or {}

    def update_response(
        self,
        message_id: str,
        agent_id: str,
        status: str,
        content: str = "",
        error: str = "",
        *,
        error_code: str = "",
        execution_provider: str = "",
        transport: str = "",
        fallback: dict[str, Any] | None = None,
        memory_candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_nexus_agent_responses
                SET status = ?, content = ?, error = ?, error_code = ?,
                    execution_provider = ?, transport = ?, fallback_json = ?,
                    memory_candidates_json = ?, updated_at = ?
                WHERE message_id = ? AND agent_id = ?
                """,
                (
                    status,
                    content,
                    error,
                    error_code,
                    execution_provider,
                    transport,
                    json.dumps(fallback or {}, ensure_ascii=False),
                    json.dumps(memory_candidates or [], ensure_ascii=False),
                    utc_now(),
                    message_id,
                    agent_id,
                ),
            )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_nexus_group_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        messages = self.list_messages(limit=1, message_id=message_id)
        return messages[0] if messages else None

    def list_messages(self, limit: int = 50, message_id: str = "") -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if message_id:
            where = "WHERE message_id = ?"
            params.append(message_id)
        params.append(max(1, min(200, limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM ai_nexus_group_messages
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in rows]
            responses: dict[str, list[dict[str, Any]]] = {key: [] for key in message_ids}
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                response_rows = connection.execute(
                    f"""
                    SELECT * FROM ai_nexus_agent_responses
                    WHERE message_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    message_ids,
                ).fetchall()
                for response in response_rows:
                    response_item = dict(response)
                    response_item["fallback"] = self._json_object(
                        response_item.pop("fallback_json", "{}")
                    )
                    response_item["memory_candidates"] = self._json_list(
                        response_item.pop("memory_candidates_json", "[]")
                    )
                    responses[str(response["message_id"])].append(response_item)
        output = []
        for row in reversed(rows):
            item = dict(row)
            item["selected_agents"] = self._json_list(item.pop("selected_agents_json", "[]"))
            item["responses"] = responses.get(str(row["message_id"]), [])
            output.append(item)
        return output

    def list_memory_items(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_nexus_memory_items ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_memory_item(self, kind: str, title: str, content: str) -> dict[str, Any]:
        now = utc_now()
        memory_id = uuid.uuid4().hex[:16]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_memory_items
                (memory_id, kind, title, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, kind, title, content, now, now),
            )
        return {
            "memory_id": memory_id,
            "kind": kind,
            "title": title,
            "content": content,
            "created_at": now,
            "updated_at": now,
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_nexus_tasks ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["participant_agents"] = self._json_list(item.pop("participant_agents_json", "[]"))
            item["files"] = self._json_list(item.pop("files_json", "[]"))
            output.append(item)
        return output

    def create_task(self, title: str, source_message_id: str = "", participant_agents: list[str] | None = None) -> dict[str, Any]:
        now = utc_now()
        task_id = uuid.uuid4().hex[:16]
        agents = participant_agents or []
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_tasks
                (task_id, title, status, source_message_id, participant_agents_json, files_json, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?, '[]', ?, ?)
                """,
                (task_id, title, source_message_id, json.dumps(agents, ensure_ascii=False), now, now),
            )
        return {
            "task_id": task_id,
            "title": title,
            "status": "pending",
            "source_message_id": source_message_id,
            "participant_agents": agents,
            "files": [],
            "conclusion": "",
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _json_list(value: str) -> list[Any]:
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
