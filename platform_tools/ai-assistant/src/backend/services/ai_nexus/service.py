from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .browser_session import AiNexusBrowserSession
from .investment_watch import InvestmentWatchService
from .repository import AiNexusRepository


class AiNexusService:
    VERSION = "0.1.0"
    AI_COMMANDS = {
        "ai_nexus_get_state",
        "ai_nexus_open_agent",
        "ai_nexus_set_agent_selection",
        "ai_nexus_send_message",
        "ai_nexus_add_memory",
        "ai_nexus_create_task",
    }
    INVESTMENT_COMMANDS = {
        "investment_watch_get_state",
        "investment_watch_import_portfolio",
        "investment_watch_clear_state",
        "investment_watch_open_agent",
        "investment_watch_run_primary_agent",
        "investment_watch_run_all_primary_agents",
        "investment_watch_run_local_risk_ai",
        "investment_watch_gemini_quote_search",
        "investment_watch_gpt_extract_filter",
        "investment_watch_run_pipeline",
    }
    COMMANDS = AI_COMMANDS | INVESTMENT_COMMANDS

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root
        self.repository = AiNexusRepository(project_root)
        self._owns_session = session is None
        self.session = session or AiNexusBrowserSession(
            profile_name="ai-assistant",
            profile_root=project_root / "runtime" / "browser-profiles",
        )
        self.investment_service = InvestmentWatchService(project_root, self.session)
        self._send_lock = asyncio.Lock()

    @property
    def workspace(self) -> Any:
        class Workspace:
            workspace_root = self.project_root / "platform_tools" / "ai-assistant"

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        if self.investment_service is not None and hasattr(self.investment_service, "start"):
            await self.investment_service.start()
        return None

    async def shutdown(self) -> None:
        if self.investment_service is not None and hasattr(self.investment_service, "shutdown"):
            await self.investment_service.shutdown()
        if self._owns_session and hasattr(self.session, "shutdown"):
            await self.session.shutdown()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        if command in self.INVESTMENT_COMMANDS:
            return await self._handle_investment_command(command, payload)

        handlers = {
            "ai_nexus_get_state": self._get_state,
            "ai_nexus_open_agent": self._open_agent,
            "ai_nexus_set_agent_selection": self._set_agent_selection,
            "ai_nexus_send_message": self._send_message,
            "ai_nexus_add_memory": self._add_memory,
            "ai_nexus_create_task": self._create_task,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {"ok": False, "message": "不支援的 AI投資管家指令"}
        try:
            return f"{command}_result", await handler(payload)
        except Exception as exc:
            return f"{command}_result", {"ok": False, "message": str(exc)}

    async def _handle_investment_command(
        self,
        command: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        event, result = await self.investment_service.handle(command, payload)
        if result.get("ok") is True:
            memory_item = self._auto_memory_from_investment_result(command, result)
            if memory_item:
                result["auto_memory_item"] = memory_item
                result["memory_items"] = self.repository.list_memory_items()
        return event, result

    async def _get_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "version": self.VERSION,
            "agents": self.repository.list_agents(),
            "messages": self.repository.list_messages(),
            "memory_items": self.repository.list_memory_items(),
            "tasks": self.repository.list_tasks(),
            "database_path": str(self.repository.db_path),
            "workspace_path": str(self.project_root / "platform_tools" / "ai-assistant"),
            "browser_profile_path": str(getattr(self.session, "shared_profile_dir", "")),
            "safety_notice": "使用 Microsoft Edge Stable 官方網頁版；遇到登入、Captcha、Cloudflare 或權限限制時會停止並要求手動處理。",
        }

    async def _open_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip()
        agents = self.repository.get_agents([agent_id])
        if not agents:
            return {"ok": False, "message": "找不到 AI Agent"}
        result = await self.session.open_agent(agents[0])
        if result.get("ok") is True:
            self.repository.update_agent_status(agent_id, "opened")
        return {"agent_id": agent_id, **result}

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

    async def _send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content", "")).strip()
        if not content:
            return {"ok": False, "message": "請輸入要送出的訊息"}
        raw_ids = payload.get("agent_ids", [])
        requested_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        agents = (
            self.repository.get_agents(requested_ids)
            if requested_ids
            else [agent for agent in self.repository.list_agents() if agent.get("selected")]
        )
        agents = [agent for agent in agents if agent.get("enabled")]
        if not agents:
            return {"ok": False, "message": "請至少選擇一個 AI"}

        agent_ids = [str(agent["agent_id"]) for agent in agents]
        async with self._send_lock:
            message = self.repository.create_group_message(content, agent_ids)
            await asyncio.gather(
                *(self._run_agent_message(message["message_id"], agent, content) for agent in agents)
            )
        group_message = self.repository.get_message(message["message_id"]) or {}
        memory_item = self._auto_memory_from_group_message(group_message)
        return {
            "ok": True,
            "message": "AI投資管家已收集回覆。",
            "group_message": group_message,
            "auto_memory_item": memory_item,
            "messages": self.repository.list_messages(),
            "agents": self.repository.list_agents(),
            "memory_items": self.repository.list_memory_items(),
        }

    async def _run_agent_message(
        self,
        message_id: str,
        agent: dict[str, Any],
        content: str,
    ) -> None:
        agent_id = str(agent["agent_id"])
        self.repository.update_agent_status(agent_id, "running")
        try:
            result = await self.session.send_prompt(agent, content)
            status = str(result.get("status", "completed") or "completed")
            response = str(result.get("content", "") or "")
            error = str(result.get("error", "") or "")
            self.repository.update_response(message_id, agent_id, status, response, error)
            self.repository.update_agent_status(agent_id, status, error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)
            self.repository.update_response(message_id, agent_id, "failed", "", error)
            self.repository.update_agent_status(agent_id, "failed", error)

    async def _add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", "note")).strip() or "note"
        title = str(payload.get("title", "")).strip()
        content = str(payload.get("content", "")).strip()
        if not content:
            return {"ok": False, "message": "記憶內容不可空白"}
        if not title:
            title = content[:40]
        item = self.repository.add_memory_item(kind, title, content)
        return {
            "ok": True,
            "memory_item": item,
            "memory_items": self.repository.list_memory_items(),
            "message": "已儲存共享記憶。",
        }

    async def _create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        if not title:
            return {"ok": False, "message": "任務標題不可空白"}
        raw_agents = payload.get("participant_agents", [])
        participant_agents = [str(item) for item in raw_agents] if isinstance(raw_agents, list) else []
        task = self.repository.create_task(
            title,
            source_message_id=str(payload.get("source_message_id", "")).strip(),
            participant_agents=participant_agents,
        )
        return {
            "ok": True,
            "task": task,
            "tasks": self.repository.list_tasks(),
            "message": "已建立任務。",
        }

    def _auto_memory_from_group_message(
        self,
        group_message: dict[str, Any],
    ) -> dict[str, Any] | None:
        content = str(group_message.get("content") or "").strip()
        if not content:
            return None
        response_lines: list[str] = []
        for response in group_message.get("responses", []):
            if not isinstance(response, dict):
                continue
            body = str(response.get("content") or response.get("error") or "").strip()
            if not body:
                continue
            response_lines.append(
                f"[{response.get('agent_id')}] {str(response.get('status') or '')}\n"
                f"{self._shorten(body, 900)}"
            )
        memory_content = "\n\n".join(
            [
                "自動共享記憶：AI 群組回覆",
                f"問題：{content}",
                "回覆摘要：",
                "\n\n".join(response_lines) if response_lines else "尚無有效回覆。",
            ]
        )
        return self.repository.add_memory_item(
            "auto",
            f"AI 群組：{self._shorten(content, 28)}",
            memory_content,
        )

    def _auto_memory_from_investment_result(
        self,
        command: str,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        if command in {"investment_watch_get_state", "investment_watch_clear_state", "investment_watch_open_agent"}:
            return None
        title = "投資管家"
        content_parts = [f"自動共享記憶：{command}", str(result.get("message") or "")]
        local_result = result.get("local_risk_ai")
        if isinstance(local_result, dict):
            local_assessment = local_result.get("local_ai_assessment")
            memory_update = (
                local_assessment.get("memory_update")
                if isinstance(local_assessment, dict)
                else None
            )
            if isinstance(memory_update, dict):
                title = str(memory_update.get("title") or title)
                content_parts.append(str(memory_update.get("content") or ""))
            command_result = local_result.get("command_result")
            if isinstance(command_result, dict) and command_result.get("text"):
                content_parts.append(str(command_result.get("text") or ""))
            elif local_result.get("content"):
                content_parts.append(self._shorten(str(local_result.get("content") or ""), 1200))
        run = result.get("run")
        if isinstance(run, dict):
            title = f"{run.get('provider') or '投資管家'}：{run.get('role') or command}"
            body = str(run.get("content") or run.get("error") or "").strip()
            if body and len(content_parts) < 3:
                content_parts.append(self._shorten(body, 1200))
        summary = result.get("summary")
        if isinstance(summary, dict):
            content_parts.append(
                "摘要："
                f"持倉 {summary.get('holding_count', '-')}, "
                f"風險 {summary.get('warning_count', '-')}, "
                f"重大 {summary.get('critical_count', '-')}"
            )
        return self.repository.add_memory_item(
            "auto-investment",
            self._shorten(title, 40),
            "\n\n".join(part for part in content_parts if part),
        )

    @staticmethod
    def _shorten(value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"
