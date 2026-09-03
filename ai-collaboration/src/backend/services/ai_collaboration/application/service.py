from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..infrastructure.repository import AiCollaborationRepository


def _resolve_tool_root(project_root: Path) -> Path:
    if project_root.name == "ai-collaboration" and (project_root / "manifest.json").is_file():
        return project_root.resolve()
    candidate = project_root / "ai-collaboration"
    if candidate.exists():
        return candidate.resolve()
    return project_root.resolve()


class AiCollaborationService:
    VERSION = "1.0.0"
    MAX_PARALLEL_AI = 6
    BROWSER_WAIT_CYCLES = 3
    BROWSER_WAIT_SECONDS = 20
    FIXED_TASK_OWNERS = {
        "general": "chatgpt",
        "orchestration": "chatgpt",
        "search": "gemini",
        "advanced_search": "perplexity",
        "calculation": "perplexity",
        "longform": "claude",
        "reasoning": "deepseek",
        "social_media": "grok",
        "trends": "grok",
        "breaking_news": "grok",
        "training-candidate-authoring": "chatgpt",
    }
    COMMANDS = {
        "ai_nexus_get_state",
        "ai_nexus_open_agent",
        "ai_nexus_authorize_agent",
        "ai_nexus_open_selected_agents",
        "ai_nexus_set_agent_selection",
        "ai_nexus_update_agent_business_settings",
        "ai_nexus_send_message",
        "ai_nexus_complete_browser_response",
        "ai_nexus_add_memory",
        "ai_nexus_create_task",
        "ai_nexus_export_report",
    }

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = _resolve_tool_root(project_root)
        self.repository = AiCollaborationRepository(self.tool_root)
        self._owns_session = session is None
        if session is None:
            from ..integration.provider_session import AiCollaborationProviderSession

            self.session = AiCollaborationProviderSession(self.project_root)
        else:
            self.session = session
        self._send_lock = asyncio.Lock()
        self._task_slots = asyncio.Semaphore(self.MAX_PARALLEL_AI)
        self._browser_completion: dict[tuple[str, str], asyncio.Future[str]] = {}

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
            "ai_nexus_authorize_agent": self._authorize_agent,
            "ai_nexus_open_selected_agents": self._open_selected_agents,
            "ai_nexus_set_agent_selection": self._set_agent_selection,
            "ai_nexus_update_agent_business_settings": self._update_agent_business_settings,
            "ai_nexus_send_message": self._send_message,
            "ai_nexus_complete_browser_response": self._complete_browser_response,
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
            "provider_execution": {
                "default_mode": "google-chrome-playwright-foreground",
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
            "safety_notice": "所有外部 AI 均使用前景 Google Chrome；工具會自動輸入、送出並擷取回覆，不使用 CLI 或 API。",
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
                f"已在同一個 AI 協作前景 Chrome 開啟 "
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

    async def _send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_by = self._requester_tool_id(payload)
        raw_tasks = payload.get("tasks")
        if requested_by == "ai-collaboration":
            if isinstance(raw_tasks, list) and raw_tasks:
                raise PermissionError("PERMISSION_DENIED")
            return await self._send_general_message(payload)
        if isinstance(raw_tasks, list) and raw_tasks:
            tasks = [
                dict(item)
                for item in raw_tasks[: self.MAX_PARALLEL_AI]
                if isinstance(item, dict)
            ]
            if not tasks:
                return {"ok": False, "message": "多工任務格式無效"}

            async def execute_task(item: dict[str, Any]) -> dict[str, Any]:
                task_payload = {key: value for key, value in payload.items() if key != "tasks"}
                task_payload["content"] = str(
                    item.get("content") or payload.get("content") or ""
                )
                task_payload["business_task"] = str(
                    item.get("task_type") or item.get("business_task") or "general"
                )
                task_payload["business_scope"] = str(
                    item.get("business_scope")
                    or payload.get("business_scope")
                    or "general"
                )
                if item.get("research_pipeline"):
                    task_payload["research_pipeline"] = item["research_pipeline"]
                async with self._task_slots:
                    return await self._send_fixed_message(task_payload)

            results = await asyncio.gather(*(execute_task(item) for item in tasks))
            return {
                "ok": all(result.get("ok") is True for result in results),
                "message": f"固定責任多工已完成 {len(results)} 條工作流。",
                "execution_mode": "parallel-fixed-responsibility",
                "maximum_parallel_tasks": self.MAX_PARALLEL_AI,
                "task_count": len(results),
                "results": results,
                "channel_coordinator": "xingcheng",
                "response_recipient": self._requester_tool_id(payload),
            }
        return await self._send_fixed_message(payload)

    async def _send_general_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the UI's user-selected general collaboration without fixed routing."""
        content = str(payload.get("content", "")).strip()
        if not content:
            return {"ok": False, "message": "請輸入要交給 AI 協作的內容"}
        business_task = str(payload.get("business_task") or "general").strip().casefold()
        if business_task != "general" or payload.get("research_pipeline"):
            raise PermissionError("PERMISSION_DENIED")

        raw_ids = payload.get("agent_ids", [])
        requested_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_ids
                if str(item).strip()
            )
        ) if isinstance(raw_ids, list) else []
        if not requested_ids:
            return {"ok": False, "message": "一般模式請至少選擇一個 AI"}
        if len(requested_ids) > self.MAX_PARALLEL_AI:
            return {
                "ok": False,
                "message": f"一般模式最多可同時選擇 {self.MAX_PARALLEL_AI} 個 AI",
            }

        known_agents = {
            str(agent["agent_id"]): agent
            for agent in self.repository.get_agents(requested_ids)
        }
        unknown_ids = [agent_id for agent_id in requested_ids if agent_id not in known_agents]
        if unknown_ids:
            raise ValueError(f"找不到指定的 AI：{', '.join(unknown_ids)}")

        business_scope = self._business_scope(payload)
        agents: list[dict[str, Any]] = []
        for agent_id in requested_ids:
            agent = known_agents[agent_id]
            if not agent.get("enabled"):
                raise ValueError(f"{agent.get('name') or agent_id} 未啟用")
            capabilities = {
                str(item).strip().casefold()
                for item in agent.get("business_capabilities", [])
            }
            if "general" not in capabilities:
                raise ValueError(f"{agent.get('name') or agent_id} 不支援一般協作")
            agents.append(self._agent_for_business(agent, business_scope))

        requested_by = self._requester_tool_id(payload)
        async with self._send_lock:
            message = self.repository.create_group_message(
                content, requested_ids, business_scope
            )

        async def run_selected_agent(agent: dict[str, Any]) -> None:
            async with self._task_slots:
                await self._run_agent_message(
                    message["message_id"],
                    agent,
                    content,
                    "general",
                    business_scope,
                    requested_by,
                    [],
                    False,
                    wait_for_browser=False,
                )

        try:
            await asyncio.gather(*(run_selected_agent(agent) for agent in agents))
        finally:
            close_background = getattr(self.session, "close_background_context", None)
            if callable(close_background):
                await close_background()

        group_message = self.repository.get_message(message["message_id"]) or {}
        memory_item = self._auto_memory_from_group_message(group_message)
        responses = [
            item for item in group_message.get("responses", []) if isinstance(item, dict)
        ]
        statuses = {str(item.get("status") or "") for item in responses}
        memory_candidates = [
            candidate
            for response in responses
            for candidate in response.get("memory_candidates", [])
            if isinstance(candidate, dict)
        ]
        awaiting_count = sum(
            str(item.get("status") or "") == "awaiting-user" for item in responses
        )
        return {
            "ok": True,
            "message": (
                f"已在 Chrome 開啟 {awaiting_count} 個 AI，請完成操作並貼回結果。"
                if awaiting_count
                else f"一般模式已收集 {len(responses)} 個 AI 回覆。"
            ),
            "group_message": group_message,
            "auto_memory_item": memory_item,
            "messages": self.repository.list_messages(),
            "agents": self.repository.list_agents(),
            "memory_items": self.repository.list_memory_items(),
            "tasks": self.repository.list_tasks(),
            "execution_mode": "parallel-user-selected-general",
            "business_task": "general",
            "selected_agents": requested_ids,
            "requested_by": requested_by,
            "response_recipient": requested_by,
            "memory_interchange": {
                "mode": "tool-local-candidate-collection",
                "direct_database_access": False,
                "candidate_count": len(memory_candidates),
                "candidates": memory_candidates,
            },
            "external_coordinator": None,
            "final_coordinator": None,
            "workflow_status": (
                "awaiting-browser-results"
                if awaiting_count
                else
                "completed"
                if responses and statuses == {"completed"}
                else "attention-required"
            ),
            "channel_coordinator": "ai-collaboration",
            "fixed_workflow": [],
        }

    async def _send_fixed_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content", "")).strip()
        if not content:
            return {"ok": False, "message": "請輸入要交給 AI 協作的內容"}
        raw_ids = payload.get("agent_ids", [])
        requested_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        business_task = str(payload.get("business_task") or "general").strip().casefold()
        requested_by = self._requester_tool_id(payload)
        if requested_by != "xingcheng":
            raise PermissionError("PERMISSION_DENIED")
        memory_context = payload.get("memory_context", [])
        memory_writeback = payload.get("memory_writeback") is not False
        task_capabilities = {
            "general": "general",
            "orchestration": "orchestration",
            "search": "search",
            "advanced_search": "advanced_search",
            "calculation": "calculation",
            "longform": "longform",
            "reasoning": "reasoning",
            "social_media": "social_media",
            "trends": "trends",
            "breaking_news": "breaking_news",
            "training-candidate-authoring": "training-candidate-authoring",
        }
        if business_task not in task_capabilities:
            raise ValueError("不支援的業務任務")
        business_scope = self._business_scope(payload)
        pipeline = str(payload.get("research_pipeline") or "").strip().casefold()
        if pipeline and pipeline != "google-gemini":
            raise ValueError("不支援的研究管線")
        if pipeline == "google-gemini":
            if business_scope != "investment":
                raise ValueError("Google-Gemini 管線只允許投資業務")
            agents = self.repository.get_agents(["google-search", "gemini"])
            providers = {str(agent.get("provider") or ""): agent for agent in agents}
            if "google-search" not in providers or "gemini" not in providers:
                raise ValueError("Google-Gemini 管線需要 Google 搜尋與 Gemini")
        else:
            fixed_owner = self.FIXED_TASK_OWNERS[business_task]
            agents = self.repository.get_agents([fixed_owner])
        agents = [agent for agent in agents if agent.get("enabled")]
        if not agents:
            return {"ok": False, "message": "固定責任 AI 未啟用"}
        agents = [self._agent_for_business(agent, business_scope) for agent in agents]

        coordinator = self.repository.get_agents(["chatgpt"])
        if not coordinator or not coordinator[0].get("enabled"):
            return {"ok": False, "message": "ChatGPT 最終統籌未啟用"}
        coordinator_agent = self._agent_for_business(coordinator[0], business_scope)
        agent_ids = [str(agent["agent_id"]) for agent in agents]
        if business_task == "training-candidate-authoring":
            coordinator_agent = self._star_training_agent(coordinator_agent)
            if str(agents[0].get("agent_id") or "") == "chatgpt":
                agents[0] = coordinator_agent
        if "chatgpt" not in agent_ids:
            agent_ids.append("chatgpt")
        async with self._send_lock:
            message = self.repository.create_group_message(
                content, agent_ids, business_scope
            )
        try:
            if pipeline == "google-gemini":
                await self._run_google_gemini_pipeline(
                    message["message_id"],
                    providers["google-search"],
                    providers["gemini"],
                    content,
                    business_scope,
                    requested_by,
                    memory_context,
                    memory_writeback,
                )
            else:
                await self._run_agent_message(
                    message["message_id"],
                    agents[0],
                    content,
                    business_task,
                    business_scope,
                    requested_by,
                    memory_context,
                    memory_writeback,
                )
            if str(agents[0].get("agent_id") or "") != "chatgpt":
                await self._run_chatgpt_final_coordination(
                    message["message_id"],
                    coordinator_agent,
                    content,
                    business_task,
                    business_scope,
                    requested_by,
                    memory_context,
                    memory_writeback,
                )
        finally:
            close_background = getattr(
                self.session, "close_background_context", None
            )
            if callable(close_background):
                await close_background()
        group_message = self.repository.get_message(message["message_id"]) or {}
        memory_item = self._auto_memory_from_group_message(group_message)
        returned_group_message = group_message
        if pipeline == "google-gemini":
            returned_group_message = dict(group_message)
            returned_group_message["selected_agents"] = ["gemini", "chatgpt"]
            returned_group_message["responses"] = [
                item
                for item in group_message.get("responses", [])
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") in {"gemini", "chatgpt"}
            ]
            returned_group_message["google_raw_results_exposed"] = False
            returned_group_message["processor"] = "gemini"
        final_response = next(
            (
                item
                for item in returned_group_message.get("responses", [])
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == "chatgpt"
            ),
            None,
        )
        memory_candidates = [
            candidate
            for response in returned_group_message.get("responses", [])
            if isinstance(response, dict)
            for candidate in response.get("memory_candidates", [])
            if isinstance(candidate, dict)
        ]
        return {
            "ok": True,
            "message": "AI 協作回覆已收集。",
            "group_message": returned_group_message,
            "auto_memory_item": memory_item,
            "messages": self.repository.list_messages(),
            "agents": self.repository.list_agents(),
            "memory_items": self.repository.list_memory_items(),
            "tasks": self.repository.list_tasks(),
            "research_pipeline": pipeline or "fixed-responsibility",
            "business_task": business_task,
            "fixed_task_owner": "gemini" if pipeline else self.FIXED_TASK_OWNERS[business_task],
            "requested_selection_ignored_for_routing": requested_ids,
            "requested_by": requested_by,
            "response_recipient": requested_by,
            "memory_interchange": {
                "mode": "star-mediated-candidate-writeback",
                "direct_database_access": False,
                "candidate_count": len(memory_candidates),
                "candidates": memory_candidates,
            },
            "external_coordinator": "chatgpt",
            "final_coordinator": "chatgpt",
            "final_response": final_response or {},
            "workflow_status": (
                "completed"
                if isinstance(final_response, dict)
                and str(final_response.get("status") or "") == "completed"
                else "awaiting-fixed-owner"
            ),
            "channel_coordinator": "xingcheng",
            "fixed_workflow": [
                "star-request",
                "fixed-owner",
                "same-provider-shared-foreground-browser-tab",
                "chatgpt-final-coordination",
                "star-response",
            ],
        }

    async def _run_chatgpt_final_coordination(
        self,
        message_id: str,
        coordinator: dict[str, Any],
        original_content: str,
        business_task: str,
        business_scope: str,
        requested_by: str,
        memory_context: Any,
        memory_writeback: bool,
    ) -> None:
        message = self.repository.get_message(message_id) or {}
        specialist_outputs = [
            {
                "agent_id": str(item.get("agent_id") or ""),
                "status": str(item.get("status") or ""),
                "execution_provider": str(item.get("execution_provider") or ""),
                "content": str(item.get("content") or ""),
                "error": str(item.get("error") or ""),
            }
            for item in message.get("responses", [])
            if isinstance(item, dict)
            and str(item.get("agent_id") or "") != "chatgpt"
        ]
        if any(item["status"] == "awaiting-user" for item in specialist_outputs):
            self.repository.update_response(
                message_id,
                "chatgpt",
                "waiting",
                "",
                "固定責任 AI 尚在等待前景瀏覽器互動",
                error_code="FIXED_OWNER_BROWSER_RESULT_REQUIRED",
                execution_provider="chatgpt",
                transport="fixed-workflow-wait",
            )
            self.repository.update_agent_status("chatgpt", "waiting")
            return
        coordination_prompt = (
            "你是所有外部 AI 工作流的最終統籌。責任 AI 已固定，不得改寫責任歸屬。"
            "請整合下列輸出，清楚標示失敗、最後備援、來源、不確定性與分歧；不得直接"
            "修改其他工具，結果只回傳星澄。\n\n"
            f"任務類型：{business_task}\n原始需求：{original_content}\n"
            f"固定責任輸出：{json.dumps(specialist_outputs, ensure_ascii=False)}"
        )
        await self._run_agent_message(
            message_id,
            coordinator,
            coordination_prompt,
            "orchestration",
            business_scope,
            requested_by,
            memory_context,
            memory_writeback,
        )

    async def _run_google_gemini_pipeline(
        self,
        message_id: str,
        google_agent: dict[str, Any],
        gemini_agent: dict[str, Any],
        query: str,
        business_scope: str,
        requested_by: str,
        memory_context: Any,
        memory_writeback: bool,
    ) -> None:
        self.repository.update_response(
            message_id,
            str(google_agent["agent_id"]),
            "delegated",
            "",
            "",
            execution_provider="gemini",
            transport="browser-automated-capability-delegation",
            fallback={
                "used": True,
                "requested_provider": "google-search",
                "effective_provider": "gemini",
            },
        )
        self.repository.update_agent_status(
            str(google_agent["agent_id"]), "delegated"
        )
        gemini_prompt = (
            "你是星澄投資研究管線中的搜尋與資料整理者。請使用 Gemini 支援的 Google "
            "搜尋能力，逐項整理配息、股價、淨值線索、來源 URL、資料日期與可信度。搜尋摘要不是"
            "最終證據；無官方或結構化來源佐證時標示『待驗證』，不得猜測，也不得直接"
            "要求修改 AI 投資管家。\n\n"
            f"原始查詢：{query}"
        )
        await self._run_agent_message(
            message_id,
            gemini_agent,
            gemini_prompt,
            "search",
            business_scope,
            requested_by,
            memory_context,
            memory_writeback,
        )

    @staticmethod
    def _business_scope(payload: dict[str, Any]) -> str:
        scope = str(payload.get("business_scope") or "general").strip().casefold()
        if scope not in {"general", "investment"}:
            raise ValueError("business_scope 僅允許 general 或 investment")
        return scope

    @staticmethod
    def _agent_for_business(
        agent: dict[str, Any], business_scope: str
    ) -> dict[str, Any]:
        enabled_key = f"{business_scope}_enabled"
        url_key = f"{business_scope}_url"
        if not bool(agent.get(enabled_key)):
            label = "投資" if business_scope == "investment" else "一般"
            raise ValueError(f"{agent.get('name') or agent.get('agent_id')} 未啟用{label}業務")
        target_url = str(agent.get(url_key) or "").strip()
        if not target_url:
            label = "投資" if business_scope == "investment" else "一般"
            raise ValueError(f"{agent.get('name') or agent.get('agent_id')} 尚未設定{label}業務 URL")
        scoped = dict(agent)
        scoped["home_url"] = target_url
        scoped["business_scope"] = business_scope
        return scoped

    @staticmethod
    def _star_training_agent(agent: dict[str, Any]) -> dict[str, Any]:
        """Route GPT's Star-training work to its dedicated conversation URL."""

        if str(agent.get("agent_id") or "") != "chatgpt":
            raise ValueError("星澄訓練 URL 僅能用於 ChatGPT")
        target_url = str(agent.get("star_training_url") or "").strip()
        if not target_url:
            raise ValueError("ChatGPT 尚未設定星澄訓練專用 URL")
        scoped = dict(agent)
        scoped["home_url"] = target_url
        scoped["conversation_scope"] = "star-training"
        return scoped

    async def _run_agent_message(
        self,
        message_id: str,
        agent: dict[str, Any],
        content: str,
        business_task: str,
        business_scope: str,
        requested_by: str,
        memory_context: Any,
        memory_writeback: bool,
        *,
        wait_for_browser: bool = True,
    ) -> None:
        agent_id = str(agent["agent_id"])
        self.repository.update_agent_status(agent_id, "running")
        try:
            send_task = getattr(self.session, "send_task", None)
            if callable(send_task):
                from ..domain.task_protocol import build_ai_task_envelope

                task = build_ai_task_envelope(
                    provider=str(agent.get("provider") or ""),
                    business_scope=business_scope,
                    task_type=business_task,
                    content=content,
                    requested_by=requested_by,
                    memory_context=memory_context,
                    memory_writeback=memory_writeback,
                )
                result = await send_task(agent, task)
            else:
                result = await self.session.send_prompt(agent, content)
            status = str(result.get("status", "completed") or "completed")
            if status == "awaiting-user" and wait_for_browser:
                result = await self._wait_for_browser_or_fallback(
                    message_id,
                    agent,
                    task if callable(send_task) else {},
                    result,
                )
                status = str(result.get("status") or "failed")
            response = str(result.get("content", "") or "")
            error = str(result.get("error", "") or "")
            self.repository.update_response(
                message_id,
                agent_id,
                status,
                response,
                error,
                error_code=str(result.get("error_code") or ""),
                execution_provider=str(
                    result.get("provider") or agent.get("provider") or ""
                ),
                transport=str(result.get("transport") or ""),
                fallback=(
                    dict(result.get("fallback"))
                    if isinstance(result.get("fallback"), dict)
                    else {}
                ),
                memory_candidates=[
                    dict(item)
                    for item in result.get("memory_candidates", [])
                    if isinstance(item, dict)
                ]
                if isinstance(result.get("memory_candidates"), list)
                else [],
            )
            self.repository.update_agent_status(agent_id, status, error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)
            self.repository.update_response(message_id, agent_id, "failed", "", error)
            self.repository.update_agent_status(agent_id, "failed", error)

    async def _wait_for_browser_or_fallback(
        self,
        message_id: str,
        agent: dict[str, Any],
        task: dict[str, Any],
        initial: dict[str, Any],
    ) -> dict[str, Any]:
        from ..domain.task_protocol import build_memory_candidate

        agent_id = str(agent.get("agent_id") or "")
        key = (message_id, agent_id)
        loop = asyncio.get_running_loop()
        completion = loop.create_future()
        self._browser_completion[key] = completion
        try:
            for attempt in range(1, self.BROWSER_WAIT_CYCLES + 1):
                self.repository.update_response(
                    message_id,
                    agent_id,
                    "awaiting-user",
                    "",
                    f"等待前景瀏覽器結果（{attempt}/{self.BROWSER_WAIT_CYCLES}）",
                    error_code="FOREGROUND_BROWSER_INTERACTION_REQUIRED",
                    execution_provider=agent_id,
                    transport="google-chrome-shared-foreground-tabs",
                    fallback={
                    "used": False,
                    "wait_attempt": attempt,
                    "maximum_wait_attempts": self.BROWSER_WAIT_CYCLES,
                    "browser_only": True,
                    "chatgpt_terminal_fallback": False,
                    },
                )
                try:
                    browser_content = await asyncio.wait_for(
                        asyncio.shield(completion),
                        timeout=self.BROWSER_WAIT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
                candidate = build_memory_candidate(
                    task,
                    source_agent_id=agent_id,
                    content=browser_content,
                )
                return {
                    "status": "completed",
                    "provider": agent_id,
                    "content": browser_content,
                    "error": "",
                    "error_code": "",
                    "transport": "google-chrome-foreground-manual-result",
                    "uses_api_key": False,
                    "memory_candidates": [candidate],
                    "fallback": {
                        "used": False,
                        "wait_attempts": attempt,
                        "cross_provider_substitution": False,
                    },
                }
            terminal_fallback = getattr(self.session, "send_terminal_fallback", None)
            if callable(terminal_fallback):
                return await terminal_fallback(
                    agent,
                    task,
                    "BROWSER_WAIT_EXHAUSTED_AFTER_THREE_ATTEMPTS",
                )
            return {
                **initial,
                "status": "failed",
                "error": "BROWSER_WAIT_EXHAUSTED_AFTER_THREE_ATTEMPTS",
                "error_code": "BROWSER_WAIT_EXHAUSTED_AFTER_THREE_ATTEMPTS",
            }
        finally:
            self._browser_completion.pop(key, None)

    async def _complete_browser_response(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        actor = str(payload.get("_authorized_requester_actor") or "").strip()
        if actor and actor not in {
            "governance/tool/ai-collaboration",
            "governance/main-system",
        }:
            raise PermissionError("PERMISSION_DENIED")
        message_id = str(payload.get("message_id") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not content:
            return {"ok": False, "message": "瀏覽器結果不可空白"}
        future = self._browser_completion.get((message_id, agent_id))
        if future is not None and not future.done():
            future.set_result(content[:64_000])
            message = "已接收瀏覽器結果，固定任務流程將繼續處理。"
        else:
            group_message = self.repository.get_message(message_id) or {}
            response = next(
                (
                    item
                    for item in group_message.get("responses", [])
                    if isinstance(item, dict)
                    and str(item.get("agent_id") or "") == agent_id
                ),
                None,
            )
            if not isinstance(response, dict) or str(response.get("status") or "") != "awaiting-user":
                return {"ok": False, "message": "目前沒有等待中的瀏覽器任務"}
            self.repository.update_response(
                message_id,
                agent_id,
                "completed",
                content[:64_000],
                "",
                error_code="",
                execution_provider=agent_id,
                transport="google-chrome-foreground-manual-result",
                fallback={
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
            )
            self.repository.update_agent_status(agent_id, "completed")
            refreshed = self.repository.get_message(message_id) or {}
            refreshed_responses = [
                item
                for item in refreshed.get("responses", [])
                if isinstance(item, dict)
            ]
            all_completed = bool(refreshed_responses) and all(
                str(item.get("status") or "") == "completed"
                for item in refreshed_responses
            )
            if all_completed:
                self._auto_memory_from_group_message(refreshed)
            message = (
                "所有瀏覽器結果已儲存。"
                if all_completed
                else "瀏覽器結果已儲存，尚有其他 AI 等待貼回。"
            )
        return {
            "ok": True,
            "message": message,
            "message_id": message_id,
            "agent_id": agent_id,
            "messages": self.repository.list_messages(),
            "agents": self.repository.list_agents(),
            "memory_items": self.repository.list_memory_items(),
        }

    @staticmethod
    def _requester_tool_id(payload: dict[str, Any]) -> str:
        actor = str(payload.get("_authorized_requester_actor") or "").strip()
        if actor == "governance/tool/ai-assistant":
            raise PermissionError("PERMISSION_DENIED")
        if actor == "governance/tool/xingcheng":
            return "xingcheng"
        if actor in {"governance/tool/ai-collaboration", "governance/main-system"}:
            return "ai-collaboration"
        # Direct calls are limited to in-process tests; governed runtimes always
        # supply a verified actor before reaching the service.
        if not actor:
            return "xingcheng"
        raise PermissionError("PERMISSION_DENIED")

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
            "message": "AI 協作工具記憶已儲存。",
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
                "AI 協作工具記憶：自動摘要",
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

        browser_status = (
            self.session.browser_status()
            if hasattr(self.session, "browser_status")
            else {
                "product": "google-chrome",
                "available": False,
                "mode": "shared-foreground-tabs",
                "automation": False,
            }
        )
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
            "browser": browser_status,
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
