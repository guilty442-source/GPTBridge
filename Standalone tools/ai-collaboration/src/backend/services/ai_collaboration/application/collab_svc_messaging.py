from __future__ import annotations

import asyncio
from typing import Any


class CollabSvcMessagingMixin:
    """Message-sending handlers for AiCollaborationService."""

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
                f"已在內建瀏覽器開啟 {awaiting_count} 個 AI，請完成操作並貼回結果。"
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
            agents = self.repository.get_agents(["gemini"])
            providers = {str(agent.get("provider") or ""): agent for agent in agents}
            if "gemini" not in providers:
                raise ValueError("Google-Gemini 管線需要 Gemini")
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
