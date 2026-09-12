from __future__ import annotations

import ipaddress
import json
import sqlite3
from typing import Any
from urllib.parse import urlparse

from .collab_repo_constants import utc_now


class CollabRepoAgentsMixin:
    """Agent CRUD and business-settings methods for AiCollaborationRepository."""

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT agent_id, name, provider, home_url, general_url, investment_url, star_training_url, general_enabled, investment_enabled, business_capabilities_json, enabled, selected, status, last_error, updated_at FROM ai_nexus_agents ORDER BY rowid"
            ).fetchall()
        return [self._agent_row(row) for row in rows]

    def get_agents(self, agent_ids: list[str]) -> list[dict[str, Any]]:
        if not agent_ids:
            return []
        placeholders = ",".join("?" for _ in agent_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT agent_id, name, provider, home_url, general_url, investment_url, star_training_url, general_enabled, investment_enabled, business_capabilities_json, enabled, selected, status, last_error, updated_at FROM ai_nexus_agents WHERE agent_id IN ({placeholders}) ORDER BY rowid",
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
