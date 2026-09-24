from __future__ import annotations

import asyncio
from typing import Any


class CollabSvcMessagingMixin:
    """Message-sending handlers for AiCollaborationService."""

    _FIXED_TASK_CAPABILITIES = {
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

    async def _send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("idempotency_key") or "").strip()
        if not key:
            return await self._send_message_inner(payload)
        deduplicated = self._send_dedupe_hit(payload)
        if deduplicated is not None:
            return deduplicated
        inflight = self._send_inflight.get(key)
        if inflight is not None:
            result = await inflight
            return {**dict(result), "deduplicated": True}
        task = asyncio.ensure_future(self._send_message_inner(payload))
        self._send_inflight[key] = task
        try:
            result = await task
        finally:
            self._send_inflight.pop(key, None)
        self._record_send_result(payload, result)
        return result

    async def _send_message_inner(self, payload: dict[str, Any]) -> dict[str, Any]:
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

        requested_ids, error = self._general_requested_ids(payload)
        if error is not None:
            return error

        known_agents = {
            str(agent["agent_id"]): agent
            for agent in self.repository.get_agents(requested_ids)
        }
        unknown_ids = [agent_id for agent_id in requested_ids if agent_id not in known_agents]
        if unknown_ids:
            raise ValueError(f"找不到指定的 AI：{', '.join(unknown_ids)}")

        business_scope = self._business_scope(payload)
        agents = self._general_selected_agents(
            requested_ids, known_agents, business_scope
        )
        requested_by = self._requester_tool_id(payload)
        request_id = str(payload.get("request_id") or "").strip()
        async with self._send_lock:
            message = self.repository.create_group_message(
                content,
                requested_ids,
                business_scope,
                request_id=request_id,
                runtime_generation=self._runtime_generation,
            )
        self._track_send(request_id, str(message.get("message_id") or ""))
        try:
            await self._run_general_agents(
                message["message_id"], agents, content, business_scope, requested_by
            )
        except asyncio.CancelledError:
            self.repository.cancel_pending_responses(message["message_id"])
            raise
        return self._general_message_result(
            message["message_id"], requested_ids, requested_by
        )

    def _general_requested_ids(
        self, payload: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any] | None]:
        raw_ids = payload.get("agent_ids", [])
        requested_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_ids
                if str(item).strip()
            )
        ) if isinstance(raw_ids, list) else []
        if not requested_ids:
            return [], {"ok": False, "message": "一般模式請至少選擇一個 AI"}
        if len(requested_ids) > self.MAX_PARALLEL_AI:
            return [], {
                "ok": False,
                "message": f"一般模式最多可同時選擇 {self.MAX_PARALLEL_AI} 個 AI",
            }
        return requested_ids, None

    def _general_selected_agents(
        self,
        requested_ids: list[str],
        known_agents: dict[str, dict[str, Any]],
        business_scope: str,
    ) -> list[dict[str, Any]]:
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
        return agents

    async def _run_general_agents(
        self,
        message_id: str,
        agents: list[dict[str, Any]],
        content: str,
        business_scope: str,
        requested_by: str,
    ) -> None:
        async def run_selected_agent(agent: dict[str, Any]) -> None:
            async with self._task_slots:
                await self._run_agent_message(
                    message_id,
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

    def _general_message_result(
        self, message_id: str, requested_ids: list[str], requested_by: str
    ) -> dict[str, Any]:
        group_message = self.repository.get_message(message_id) or {}
        memory_item = self._auto_memory_from_group_message(group_message)
        responses = [
            item for item in group_message.get("responses", []) if isinstance(item, dict)
        ]
        statuses = {str(item.get("status") or "") for item in responses}
        memory_candidates = self._group_memory_candidates(group_message)
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
        if business_task not in self._FIXED_TASK_CAPABILITIES:
            raise ValueError("不支援的業務任務")
        business_scope = self._business_scope(payload)
        pipeline = str(payload.get("research_pipeline") or "").strip().casefold()
        if pipeline and pipeline != "google-gemini":
            raise ValueError("不支援的研究管線")
        agents, providers = self._fixed_pipeline_agents(
            pipeline, business_task, business_scope
        )
        agents = [agent for agent in agents if agent.get("enabled")]
        if not agents:
            return {"ok": False, "message": "固定責任 AI 未啟用"}
        agents = [self._agent_for_business(agent, business_scope) for agent in agents]

        coordinator_agent, agent_ids = self._fixed_coordinator_agent(
            business_scope, business_task, agents
        )
        if coordinator_agent is None:
            return {"ok": False, "message": "ChatGPT 最終統籌未啟用"}
        request_id = str(payload.get("request_id") or "").strip()
        async with self._send_lock:
            message = self.repository.create_group_message(
                content,
                agent_ids,
                business_scope,
                request_id=request_id,
                runtime_generation=self._runtime_generation,
            )
        self._track_send(request_id, str(message.get("message_id") or ""))
        await self._run_fixed_workflow(
            message["message_id"],
            pipeline,
            providers,
            agents,
            coordinator_agent,
            content,
            business_task,
            business_scope,
            requested_by,
            memory_context,
            memory_writeback,
        )
        return self._fixed_message_result(
            message["message_id"], pipeline, business_task, requested_ids, requested_by
        )

    def _fixed_pipeline_agents(
        self, pipeline: str, business_task: str, business_scope: str
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        providers: dict[str, dict[str, Any]] = {}
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
        return agents, providers

    def _fixed_coordinator_agent(
        self,
        business_scope: str,
        business_task: str,
        agents: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        coordinator = self.repository.get_agents(["chatgpt"])
        if not coordinator or not coordinator[0].get("enabled"):
            return None, []
        coordinator_agent = self._agent_for_business(coordinator[0], business_scope)
        agent_ids = [str(agent["agent_id"]) for agent in agents]
        if business_task == "training-candidate-authoring":
            coordinator_agent = self._star_training_agent(coordinator_agent)
            if str(agents[0].get("agent_id") or "") == "chatgpt":
                agents[0] = coordinator_agent
        if "chatgpt" not in agent_ids:
            agent_ids.append("chatgpt")
        return coordinator_agent, agent_ids

    async def _run_fixed_workflow(
        self, message_id: str, pipeline: str,
        providers: dict[str, dict[str, Any]], agents: list[dict[str, Any]],
        coordinator_agent: dict[str, Any], content: str, business_task: str,
        business_scope: str, requested_by: str, memory_context: Any,
        memory_writeback: bool,
    ) -> None:
        try:
            await self._run_fixed_owner_step(
                message_id, pipeline, providers, agents, content,
                business_task, business_scope, requested_by,
                memory_context, memory_writeback,
            )
            if str(agents[0].get("agent_id") or "") != "chatgpt":
                await self._run_chatgpt_final_coordination(
                    message_id, coordinator_agent, content, business_task,
                    business_scope, requested_by, memory_context, memory_writeback,
                )
        except asyncio.CancelledError:
            self.repository.cancel_pending_responses(message_id)
            raise
        finally:
            close_background = getattr(
                self.session, "close_background_context", None
            )
            if callable(close_background):
                await close_background()

    async def _run_fixed_owner_step(
        self, message_id: str, pipeline: str,
        providers: dict[str, dict[str, Any]], agents: list[dict[str, Any]],
        content: str, business_task: str, business_scope: str,
        requested_by: str, memory_context: Any, memory_writeback: bool,
    ) -> None:
        if pipeline == "google-gemini":
            await self._run_google_gemini_pipeline(
                message_id, providers["gemini"], content, business_scope,
                requested_by, memory_context, memory_writeback,
            )
        else:
            await self._run_agent_message(
                message_id, agents[0], content, business_task,
                business_scope, requested_by, memory_context, memory_writeback,
            )

    def _fixed_message_result(
        self,
        message_id: str,
        pipeline: str,
        business_task: str,
        requested_ids: list[str],
        requested_by: str,
    ) -> dict[str, Any]:
        group_message = self.repository.get_message(message_id) or {}
        memory_item = self._auto_memory_from_group_message(group_message)
        returned_group_message = (
            self._gemini_filtered_group_message(group_message)
            if pipeline == "google-gemini"
            else group_message
        )
        return self._fixed_result_payload(
            returned_group_message,
            memory_item,
            pipeline,
            business_task,
            requested_ids,
            requested_by,
        )

    def _fixed_result_payload(
        self,
        returned_group_message: dict[str, Any],
        memory_item: Any,
        pipeline: str,
        business_task: str,
        requested_ids: list[str],
        requested_by: str,
    ) -> dict[str, Any]:
        final_response = self._final_chatgpt_response(returned_group_message)
        memory_candidates = self._group_memory_candidates(returned_group_message)
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

    @staticmethod
    def _final_chatgpt_response(
        group_message: dict[str, Any],
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in group_message.get("responses", [])
                if isinstance(item, dict)
                and str(item.get("agent_id") or "") == "chatgpt"
            ),
            None,
        )

    def _gemini_filtered_group_message(
        self, group_message: dict[str, Any]
    ) -> dict[str, Any]:
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
        return returned_group_message

    def _group_memory_candidates(
        self, group_message: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            candidate
            for response in group_message.get("responses", [])
            if isinstance(response, dict)
            for candidate in response.get("memory_candidates", [])
            if isinstance(candidate, dict)
        ]
