from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .browser_session import AiCollaborationBrowserSession
from .repository import AiCollaborationRepository


def _resolve_tool_root(project_root: Path) -> Path:
    candidate = project_root / "platform_tools" / "ai-collaboration"
    if candidate.exists():
        return candidate.resolve()
    return project_root.resolve()


class AiCollaborationService:
    VERSION = "1.0.0"
    COMMANDS = {
        "ai_nexus_get_state",
        "ai_nexus_open_agent",
        "ai_nexus_open_selected_agents",
        "ai_nexus_set_agent_selection",
        "ai_nexus_send_message",
        "ai_nexus_add_memory",
        "ai_nexus_create_task",
        "ai_nexus_export_report",
    }

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = _resolve_tool_root(project_root)
        self.repository = AiCollaborationRepository(self.tool_root)
        self._owns_session = session is None
        self.session = session or AiCollaborationBrowserSession(
            profile_name="ai-collaboration",
            profile_root=self.project_root / "runtime" / "browser-profiles",
        )
        self._send_lock = asyncio.Lock()

    @property
    def workspace(self) -> Any:
        tool_root = self.tool_root

        class Workspace:
            workspace_root = tool_root

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        if self._owns_session and hasattr(self.session, "shutdown"):
            await self.session.shutdown()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        handlers = {
            "ai_nexus_get_state": self._get_state,
            "ai_nexus_open_agent": self._open_agent,
            "ai_nexus_open_selected_agents": self._open_selected_agents,
            "ai_nexus_set_agent_selection": self._set_agent_selection,
            "ai_nexus_send_message": self._send_message,
            "ai_nexus_add_memory": self._add_memory,
            "ai_nexus_create_task": self._create_task,
            "ai_nexus_export_report": self._export_report,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {"ok": False, "message": "不支援的 AI 協作指令"}
        try:
            return f"{command}_result", await handler(payload)
        except Exception as exc:
            return f"{command}_result", {"ok": False, "message": str(exc)}

    async def _get_state(self, _payload: dict[str, Any]) -> dict[str, Any]:
        agents = self.repository.list_agents()
        messages = self.repository.list_messages()
        memory_items = self.repository.list_memory_items()
        tasks = self.repository.list_tasks()
        diagnostics = self._diagnostics(agents, messages, memory_items, tasks)
        return {
            "ok": True,
            "version": self.VERSION,
            "browser_execution": {
                "default_mode": "background",
                "release_after_request": True,
                "uses_external_api": False,
            },
            "agents": agents,
            "messages": messages,
            "memory_items": memory_items,
            "tasks": tasks,
            "diagnostics": diagnostics,
            "database_path": str(self.repository.db_path),
            "workspace_path": str(self.tool_root),
            "browser_profile_path": str(getattr(self.session, "shared_profile_dir", "")),
            "safety_notice": "AI 協作工具會使用獨立 Microsoft Edge 工作階段；登入、驗證與外部網站狀態不會混入 AI投資管家。",
        }

    async def _open_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip()
        agents = self.repository.get_agents([agent_id])
        if not agents:
            return {"ok": False, "message": "找不到指定的 AI"}
        result = await self.session.open_agent(agents[0])
        if result.get("ok") is True:
            self.repository.update_agent_status(agent_id, "opened")
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
            "message": f"已開啟 {opened} / {len(agents)} 個 AI。",
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

    async def _send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content", "")).strip()
        if not content:
            return {"ok": False, "message": "請輸入要交給 AI 協作的內容"}
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
            try:
                await asyncio.gather(
                    *(
                        self._run_agent_message(message["message_id"], agent, content)
                        for agent in agents
                    )
                )
            finally:
                close_background = getattr(
                    self.session, "close_background_context", None
                )
                if callable(close_background):
                    await close_background()
        group_message = self.repository.get_message(message["message_id"]) or {}
        memory_item = self._auto_memory_from_group_message(group_message)
        return {
            "ok": True,
            "message": "AI 協作回覆已收集。",
            "group_message": group_message,
            "auto_memory_item": memory_item,
            "messages": self.repository.list_messages(),
            "agents": self.repository.list_agents(),
            "memory_items": self.repository.list_memory_items(),
            "tasks": self.repository.list_tasks(),
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
            "message": "共享記憶已儲存。",
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
            "message": "任務已建立。",
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
                "自動共享記憶：AI 協作",
                f"需求：{content}",
                "回覆摘要：",
                "\n\n".join(response_lines) if response_lines else "尚無可用回覆。",
            ]
        )
        return self.repository.add_memory_item(
            "auto",
            f"AI 協作：{self._shorten(content, 28)}",
            memory_content,
        )

    @staticmethod
    def _shorten(value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    def _diagnostics(
        self,
        agents: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        memory_items: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_agents = [agent for agent in agents if agent.get("selected")]
        enabled_agents = [agent for agent in agents if agent.get("enabled")]
        status_counts: dict[str, int] = {}
        for agent in agents:
            status = str(agent.get("status") or "idle")
            status_counts[status] = status_counts.get(status, 0) + 1

        failed_responses = 0
        waiting_verification = 0
        completed_responses = 0
        latest_message = messages[-1] if messages else None
        for message in messages:
            for response in message.get("responses", []):
                if not isinstance(response, dict):
                    continue
                status = str(response.get("status") or "")
                if status == "failed":
                    failed_responses += 1
                elif status == "waiting_verification":
                    waiting_verification += 1
                elif status == "completed":
                    completed_responses += 1

        if failed_responses or waiting_verification:
            state = "attention"
            message = "有外部 AI 需要檢查登入、驗證或回覆錯誤。"
        elif status_counts.get("running"):
            state = "running"
            message = "AI 協作正在執行。"
        elif not selected_agents:
            state = "setup"
            message = "尚未選擇要協作的 AI。"
        elif enabled_agents:
            state = "ready"
            message = "AI 協作工具已就緒。"
        else:
            state = "empty"
            message = "沒有可用 AI。"

        pages = getattr(self.session, "pages", {})
        page_count = len(pages) if isinstance(pages, dict) else 0
        return {
            "state": state,
            "message": message,
            "agents": {
                "total": len(agents),
                "selected": len(selected_agents),
                "enabled": len(enabled_agents),
                "status_counts": status_counts,
            },
            "collaboration": {
                "message_count": len(messages),
                "memory_count": len(memory_items),
                "task_count": len(tasks),
                "completed_responses": completed_responses,
                "failed_responses": failed_responses,
                "waiting_verification": waiting_verification,
                "latest_message_id": latest_message.get("message_id") if latest_message else "",
            },
            "browser": {
                "initialized": bool(getattr(self.session, "is_initialized", False)),
                "profile_path": str(getattr(self.session, "shared_profile_dir", "")),
                "page_count": page_count,
            },
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    async def _export_report(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = await self._get_state({})
        export_dir = self.tool_root / "runtime" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = export_dir / f"ai-collaboration-diagnostic-{timestamp}.json"
        report_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "report_path": str(report_path),
            "message": "AI 協作診斷報告已匯出。",
        }
