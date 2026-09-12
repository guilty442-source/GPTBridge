from __future__ import annotations

import asyncio
from typing import Any


class CollabSvcBrowserMixin:
    """Browser-coordination and agent-message execution for AiCollaborationService."""

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
                    transport="embedded-browser-view",
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
                    "transport": "embedded-browser-view",
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
                transport="embedded-browser-view",
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
