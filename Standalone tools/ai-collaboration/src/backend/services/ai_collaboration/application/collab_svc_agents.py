from __future__ import annotations

from typing import Any


class CollabSvcAgentsMixin:
    """Agent management handlers for AiCollaborationService."""

    async def _get_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        agents = self.repository.list_agents()
        messages = self.repository.list_messages()
        memory_items = self.repository.list_memory_items()
        tasks = self.repository.list_tasks()
        diagnostics = self._diagnostics(agents, messages, memory_items, tasks)
        return {
            "ok": True,
            "version": self.VERSION,
            "provider_execution": {
                "default_mode": "embedded-browser-view",
                "release_after_request": True,
                "uses_external_api": False,
                "browser_automation_for_inference": True,
                "browser_only": True,
                "terminal_fallback": False,
                "task_planner": "star-main-native-model-only",
                "fixed_task_owners": dict(self.FIXED_TASK_OWNERS),
                "maximum_parallel_ai": self.MAX_PARALLEL_AI,
                "browser_wait_cycles_before_chatgpt": self.BROWSER_WAIT_CYCLES,
                "final_coordinator": "chatgpt-always",
                "providers": (
                    self.session.provider_status()
                    if hasattr(self.session, "provider_status")
                    else []
                ),
            },
            "agents": agents,
            "messages": messages,
            "memory_items": memory_items,
            "tasks": tasks,
            "diagnostics": diagnostics,
            "database_path": str(self.repository.db_path),
            "workspace_path": str(self.tool_root),
            "safety_notice": "所有外部 AI 均使用內建瀏覽器（受治理 Embedded BrowserView）；工具會自動輸入、送出並擷取回覆，不使用 CLI 或 API。",
        }

    async def _open_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip()
        agents = self.repository.get_agents([agent_id])
        if not agents:
            return {"ok": False, "message": "找不到指定的 AI"}
        business_scope = self._business_scope(payload)
        agent = self._agent_for_business(agents[0], business_scope)
        result = await self.session.open_agent(agent)
        if result.get("ok") is True:
            self.repository.update_agent_status(agent_id, "opened")
        return {"agent_id": agent_id, **result}

    async def _authorize_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip()
        agents = self.repository.get_agents([agent_id])
        if not agents:
            return {"ok": False, "message": "找不到指定的 AI"}
        authorize_agent = getattr(self.session, "authorize_agent", None)
        if not callable(authorize_agent):
            return {"ok": False, "message": "目前執行環境不支援帳號授權"}
        result = await authorize_agent(agents[0])
        if result.get("ok") is True:
            self.repository.update_agent_status(
                agent_id,
                "opened" if result.get("interactive_browser") else "authorizing",
            )
        return {"agent_id": agent_id, **result}

    async def _open_selected_agents(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ids = payload.get("agent_ids", [])
        requested_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        agents = (
            self.repository.get_agents(requested_ids)
            if requested_ids
            else [
                agent
                for agent in self.repository.list_agents()
                if agent.get("selected") and agent.get("enabled")
            ]
        )
        if not agents:
            return {"ok": False, "message": "請至少選擇一個 AI"}
        business_scope = self._business_scope(payload)
        agents = [self._agent_for_business(agent, business_scope) for agent in agents]

        results: list[dict[str, Any]] = []
        opened = 0
        for agent in agents:
            agent_id = str(agent.get("agent_id") or "")
            try:
                result = await self.session.open_agent(agent)
                if result.get("ok") is True:
                    opened += 1
                    self.repository.update_agent_status(agent_id, "opened")
                else:
                    self.repository.update_agent_status(
                        agent_id,
                        "failed",
                        str(result.get("message") or result.get("error") or ""),
                    )
                results.append({"agent_id": agent_id, **result})
            except Exception as exc:
                self.repository.update_agent_status(agent_id, "failed", str(exc))
                results.append({"agent_id": agent_id, "ok": False, "message": str(exc)})
        state = await self._get_state({})
        return {
            **state,
            "ok": opened > 0,
            "opened": opened,
            "results": results,
            "message": (
                f"已在內建瀏覽器開啟 "
                f"{opened} / {len(agents)} 個 AI 分頁。"
            ),
        }

    async def _set_agent_selection(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ids = payload.get("agent_ids", [])
        agent_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        known = {agent["agent_id"] for agent in self.repository.list_agents()}
        selected = [agent_id for agent_id in agent_ids if agent_id in known]
        self.repository.save_agent_selection(selected)
        return {
            "ok": True,
            "agents": self.repository.list_agents(),
            "selected_count": len(selected),
            "message": f"已選擇 {len(selected)} 個 AI。",
        }

    async def _update_agent_business_settings(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        agent = self.repository.save_agent_business_settings(
            str(payload.get("agent_id") or "").strip(),
            general_url=str(payload.get("general_url") or "").strip(),
            investment_url=str(payload.get("investment_url") or "").strip(),
            star_training_url=(
                str(payload["star_training_url"]).strip()
                if "star_training_url" in payload
                else None
            ),
            general_enabled=payload.get("general_enabled") is not False,
            investment_enabled=payload.get("investment_enabled") is not False,
            business_capabilities=[
                str(item)
                for item in payload.get("business_capabilities", [])
                if isinstance(item, str)
            ]
            if isinstance(payload.get("business_capabilities"), list)
            else [],
        )
        return {
            "ok": True,
            "agent": agent,
            "agents": self.repository.list_agents(),
            "message": f"{agent['name']} 的業務 URL 已儲存。",
        }
