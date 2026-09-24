"""COLLABORATION_ORCHESTRATOR — multi-provider collaboration modes.

Modes:
- ``single``             one request → one provider
- ``compare``            same request → N providers in parallel → compare
- ``sequential_review``  provider k receives the request plus every prior
                         reply; the last pass produces the integrated result

The orchestrator never acquires permissions, never touches another tool's
private data and never bypasses the Information Layer; it only drives the
governed embedded-browser provider runtimes.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ..domain.external_content import (
    EXTERNAL_CONTENT_CLASS,
    seal_provider_response,
)
from ..domain.result_comparator import CollaborationResultComparator
from ..domain.result_synthesizer import CollaborationResultSynthesizer
from ..infrastructure.collab_repo_constants import utc_now

COLLAB_MODES = frozenset({"single", "compare", "sequential_review"})


class CollabOrchestratorMixin:
    """Multi-provider collaboration lifecycle for AiCollaborationService."""

    # ------------------------------------------------------------------
    # command handlers
    # ------------------------------------------------------------------
    async def _collab_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        requester = self._requester_tool_id(payload)
        if requester not in {"ai-collaboration", "xingcheng"}:
            raise PermissionError("PERMISSION_DENIED")

        key = str(payload.get("idempotency_key") or "").strip()
        if key:
            deduplicated = self._send_dedupe_hit(payload)
            if deduplicated is not None:
                return deduplicated
            inflight = self._send_inflight.get(key)
            if inflight is not None:
                result = await inflight
                return {**dict(result), "deduplicated": True}
            task = asyncio.ensure_future(self._collab_start_inner(payload))
            self._send_inflight[key] = task
            try:
                result = await task
            finally:
                self._send_inflight.pop(key, None)
            self._record_send_result(payload, result)
            return result
        return await self._collab_start_inner(payload)

    async def _collab_start_inner(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content") or "").strip()
        if not content:
            return {"ok": False, "message": "請輸入要交給 AI 協作的內容"}
        mode = str(payload.get("mode") or "single").strip().casefold()
        if mode not in COLLAB_MODES:
            return {"ok": False, "error_code": "REQUEST_REJECTED",
                    "message": f"不支援的協作模式：{mode}"}
        raw_ids = payload.get("provider_ids") or payload.get("agent_ids") or []
        provider_ids = list(
            dict.fromkeys(
                str(item).strip().casefold()
                for item in raw_ids
                if str(item).strip()
            )
        ) if isinstance(raw_ids, list) else []
        minimum = 1 if mode == "single" else 2
        if len(provider_ids) < minimum:
            label = {"single": "一個", "compare": "兩個", "sequential_review": "兩個"}[mode]
            return {"ok": False, "message": f"此模式至少需要 {label} AI"}
        if mode == "single":
            provider_ids = provider_ids[:1]
        if len(provider_ids) > self.MAX_PARALLEL_AI:
            provider_ids = provider_ids[: self.MAX_PARALLEL_AI]

        known = {
            str(agent.get("provider") or "").strip().casefold(): agent
            for agent in self.repository.list_agents()
            if agent.get("enabled")
        }
        missing = [pid for pid in provider_ids if pid not in known]
        if missing:
            return {"ok": False, "error_code": "UNSUPPORTED_BROWSER_PROVIDER",
                    "message": f"未登錄或未啟用的 Provider：{', '.join(missing)}"}
        agents = [known[pid] for pid in provider_ids]

        request_id = str(payload.get("request_id") or "").strip() or uuid.uuid4().hex[:12]
        task = self.repository.create_collab_task(
            request_id=request_id,
            mode=mode,
            selected_providers=provider_ids,
            original_request=content,
            task_generation=self._runtime_generation,
            attempt_id=uuid.uuid4().hex[:8],
        )
        task_id = str(task["task_id"])
        self._collab_tasks[task_id] = {
            "request_id": request_id,
            "cancelled": False,
        }
        self._track_send(request_id, task_id)
        try:
            await self._run_collab_task(task_id, mode, agents, content, request_id)
        except asyncio.CancelledError:
            self.repository.update_collab_task(
                task_id, status="cancelled",
                fault_reference="REQUEST_CANCELLED", completed=True,
            )
            raise
        return self._collab_task_result(task_id)

    async def _collab_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload.get("task_id") or "").strip()
        request_id = str(payload.get("request_id") or "").strip()
        task = None
        if task_id:
            task = self.repository.get_collab_task(task_id)
        elif request_id:
            task = next(
                (
                    item
                    for item in self.repository.list_collab_tasks()
                    if str(item.get("request_id") or "") == request_id
                ),
                None,
            )
        if not task:
            return {"ok": False, "error_code": "SESSION_NOT_FOUND",
                    "message": "找不到指定的協作任務"}
        task_id = str(task["task_id"])
        tracking = self._collab_tasks.get(task_id)
        if tracking is not None:
            tracking["cancelled"] = True
        # Ask each provider runtime to stop; remote generation may be
        # unconfirmable — that is recorded, not hidden.
        pool = self._provider_pool()
        cancelled_providers: list[str] = []
        for provider_id in task.get("selected_providers", []):
            runtime = pool.runtime_for(provider_id) if pool else None
            if runtime is not None:
                await runtime.cancel(request_id=str(task.get("request_id") or ""))
                cancelled_providers.append(provider_id)
        for item in self.repository.list_collab_results(task_id):
            if str(item.get("response_status") or "") in {
                "pending", "running", "awaiting-user",
            }:
                record = dict(item)
                record["response_status"] = "cancelled"
                record["completion_evidence"] = "cancel_remote_state=unconfirmed"
                self.repository.upsert_collab_result(record)
        self.repository.update_collab_task(
            task_id, status="cancelled",
            fault_reference="REQUEST_CANCELLED", completed=True,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "cancelled_providers": cancelled_providers,
            "cancel_remote_state": "unconfirmed",
            "message": "協作任務已取消；外部頁面是否已停止生成無法確認。",
            "task": self.repository.get_collab_task(task_id),
        }

    async def _collab_manual_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Manual import path for providers whose page cannot be captured
        programmatically — recorded as capture_method=MANUAL, never AUTO."""
        actor = str(payload.get("_authorized_requester_actor") or "").strip()
        if actor and actor not in {
            "governance/tool/ai-collaboration",
            "governance/main-system",
        }:
            raise PermissionError("PERMISSION_DENIED")
        task_id = str(payload.get("task_id") or "").strip()
        provider_id = str(payload.get("provider_id") or "").strip().casefold()
        content = str(payload.get("content") or "").strip()
        if not task_id or not provider_id or not content:
            return {"ok": False, "message": "task_id / provider_id / content 必填"}
        task = self.repository.get_collab_task(task_id)
        if not task:
            return {"ok": False, "error_code": "SESSION_NOT_FOUND",
                    "message": "找不到指定的協作任務"}
        record = seal_provider_response(
            provider_id,
            str(task.get("request_id") or ""),
            uuid.uuid4().hex[:12],
            content,
            capture_method="MANUAL",
            completion_evidence="user-confirmed-import",
            adapter_version="",
            captured_at=utc_now(),
        )
        record["task_id"] = task_id
        record["attempt_id"] = str(task.get("attempt_id") or "")
        self.repository.upsert_collab_result(record)
        await self._aggregate_if_terminal(task_id)
        return {
            "ok": True,
            "message": "已匯入手動回覆（MANUAL）。",
            "task": self.repository.get_collab_task(task_id),
        }

    async def _collab_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resume a task with a fresh attempt identity; providers that
        already completed are never re-sent."""
        task_id = str(payload.get("task_id") or "").strip()
        task = self.repository.get_collab_task(task_id)
        if not task:
            return {"ok": False, "error_code": "SESSION_NOT_FOUND",
                    "message": "找不到指定的協作任務"}
        if str(task.get("overall_status") or "") == "completed":
            return {"ok": True, "task": task, "message": "任務已完成，無需重送。"}
        mode = str(task.get("mode") or "single")
        providers = list(task.get("selected_providers") or [])
        known = {
            str(agent.get("provider") or "").strip().casefold(): agent
            for agent in self.repository.list_agents()
            if agent.get("enabled")
        }
        agents = [known[pid] for pid in providers if pid in known]
        attempt_id = uuid.uuid4().hex[:8]
        # Keep completed replies: they carry the old attempt_id, so the new
        # attempt only sends providers that never finished.
        done = {
            str(item.get("provider_id") or "")
            for item in self.repository.list_collab_results(task_id)
            if str(item.get("response_status") or "") == "completed"
        }
        pending_agents = [
            agent
            for agent in agents
            if str(agent.get("provider") or "").strip().casefold() not in done
        ]
        request_id = str(task.get("request_id") or "") or uuid.uuid4().hex[:12]
        self.repository.update_collab_task(
            task_id, status="running", attempt_id=attempt_id,
            fault_reference="", started=True,
        )
        self._collab_tasks[task_id] = {"request_id": request_id, "cancelled": False}
        try:
            await self._run_collab_task(
                task_id, mode, pending_agents,
                str(task.get("original_request") or ""), request_id,
                attempt_id=attempt_id,
            )
        except asyncio.CancelledError:
            self.repository.update_collab_task(
                task_id, status="cancelled",
                fault_reference="REQUEST_CANCELLED", completed=True,
            )
            raise
        return self._collab_task_result(task_id)

    # ------------------------------------------------------------------
    # task pipelines
    # ------------------------------------------------------------------
    async def _run_collab_task(
        self,
        task_id: str,
        mode: str,
        agents: list[dict[str, Any]],
        content: str,
        request_id: str,
        *,
        attempt_id: str = "",
    ) -> None:
        attempt = attempt_id or str(
            (self.repository.get_collab_task(task_id) or {}).get("attempt_id")
            or ""
        )
        self.repository.update_collab_task(task_id, status="running", started=True)
        if mode == "single":
            await self._run_provider_step(
                task_id, request_id, attempt, agents[0], content
            )
        elif mode == "compare":
            await asyncio.gather(
                *(
                    self._run_provider_step(
                        task_id, request_id, attempt, agent, content
                    )
                    for agent in agents
                )
            )
        elif mode == "sequential_review":
            prior: list[dict[str, Any]] = []
            for index, agent in enumerate(agents):
                prompt = content if index == 0 else self._review_prompt(
                    content, prior
                )
                await self._run_provider_step(
                    task_id, request_id, attempt, agent, prompt
                )
                if self._collab_tasks.get(task_id, {}).get("cancelled"):
                    break
                latest = self.repository.list_collab_results(task_id)
                prior = [
                    item for item in latest
                    if str(item.get("response_status") or "") == "completed"
                ]
        await self._aggregate_if_terminal(task_id)

    async def _run_provider_step(
        self,
        task_id: str,
        request_id: str,
        attempt_id: str,
        agent: dict[str, Any],
        prompt: str,
    ) -> dict[str, Any]:
        provider = str(agent.get("provider") or "").strip().casefold()
        if self._collab_tasks.get(task_id, {}).get("cancelled"):
            return {"status": "cancelled", "provider": provider}
        pool = self._provider_pool()
        runtime = pool.runtime_for(
            provider, registered_url=str(agent.get("home_url") or "")
        ) if pool else None
        if runtime is None:
            result = {
                "status": "failed",
                "provider": provider,
                "content": "",
                "error_code": "UNSUPPORTED_BROWSER_PROVIDER",
                "error": "UNSUPPORTED_BROWSER_PROVIDER",
            }
        else:
            try:
                result = await runtime.submit(
                    agent, prompt, request_id=f"{task_id}:{provider}"
                )
            except asyncio.CancelledError:
                result = {
                    "status": "cancelled",
                    "provider": provider,
                    "content": "",
                    "error_code": "REQUEST_CANCELLED",
                }
            except Exception as exc:  # provider failure stays contained
                result = {
                    "status": "failed",
                    "provider": provider,
                    "content": "",
                    "error_code": "EMBEDDED_BROWSER_SESSION_FAILED",
                    "error": str(exc),
                }
        status = str(result.get("status") or "failed")
        content = str(result.get("content") or "")
        capture_method = (
            "AUTO"
            if status == "completed" and content
            else "NONE"
        )
        record = seal_provider_response(
            provider,
            request_id,
            uuid.uuid4().hex[:12],
            content,
            capture_method=capture_method,
            completion_evidence=str(
                result.get("response_state")
                or result.get("browser_handoff", {}).get("response_state")
                or status
            ),
            adapter_version=str(result.get("adapter_version") or ""),
            captured_at=utc_now(),
            status=(
                "completed" if status == "completed"
                else "cancelled" if status == "cancelled"
                else "awaiting-user" if status in {"awaiting-user", "waiting_verification"}
                else "failed"
            ),
        )
        if status != "completed":
            record["response_text"] = ""
        record["task_id"] = task_id
        record["attempt_id"] = attempt_id
        record["error_code"] = str(result.get("error_code") or "")
        self.repository.upsert_collab_result(record)
        return result

    async def _aggregate_if_terminal(self, task_id: str) -> None:
        """Compare + synthesize once every provider reached a terminal or
        user-actionable state.  PARTIAL is never disguised as COMPLETED."""
        task = self.repository.get_collab_task(task_id)
        if not task:
            return
        if str(task.get("overall_status") or "") == "cancelled":
            return  # a cancelled task is terminal; never re-open it
        results = self.repository.list_collab_results(task_id)
        statuses = {
            str(item.get("response_status") or "") for item in results
        }
        awaiting = statuses & {"awaiting-user"}
        if awaiting and not self._collab_tasks.get(task_id, {}).get("cancelled"):
            self.repository.update_collab_task(task_id, status="waiting_user")
            return
        completed = [
            item for item in results
            if str(item.get("response_status") or "") == "completed"
        ]
        self.repository.update_collab_task(task_id, status="aggregating")
        comparison = self._comparator.compare(task_id, results)
        synthesis = await self._synthesizer.synthesize(
            task_id,
            str(task.get("original_request") or ""),
            results,
            comparison,
        )
        expected = len(task.get("selected_providers") or [])
        if not completed:
            status = "failed"
        elif len(completed) < expected:
            status = "partial"
        else:
            status = "completed"
        self.repository.update_collab_task(
            task_id,
            status=status,
            comparison=comparison,
            synthesis=synthesis,
            summary_reference=f"collab_task:{task_id}:synthesis",
            completed=True,
        )

    @staticmethod
    def _review_prompt(
        original: str, prior: list[dict[str, Any]]
    ) -> str:
        lines = [
            "以下是原始需求與前序 AI 的回覆。請檢查、補充並指出遺漏或錯誤：",
            f"原始需求：{original}",
            "",
        ]
        for item in prior:
            provider = str(item.get("provider_id") or "")
            text = str(item.get("response_text") or "")[:4000]
            lines.append(f"【{provider} 的回覆】\n{text}\n")
        return "\n".join(lines)

    def _collab_task_result(self, task_id: str) -> dict[str, Any]:
        task = self.repository.get_collab_task(task_id) or {}
        status = str(task.get("overall_status") or "")
        return {
            "ok": status in {"completed", "partial", "waiting_user"},
            "task_id": task_id,
            "task": task,
            "overall_status": status,
            "mode": task.get("mode"),
            "provider_results": task.get("provider_results") or [],
            "comparison": task.get("comparison") or {},
            "synthesis": task.get("synthesis") or {},
            "collab_tasks": self.repository.list_collab_tasks(),
            "messages": self.repository.list_messages(),
            "agents": self.repository.list_agents(),
            "message": {
                "completed": "協作任務已完成。",
                "partial": "部分 AI 完成；已保留可用結果。",
                "waiting_user": "部分 AI 需要瀏覽器操作或手動匯入回覆。",
                "failed": "所有 Provider 皆失敗。",
                "cancelled": "協作任務已取消。",
            }.get(status, "協作任務已更新。"),
        }

    # ------------------------------------------------------------------
    # shared resources (wired by AiCollaborationService.__init__)
    # ------------------------------------------------------------------
    def _provider_pool(self) -> Any:
        pool = getattr(self, "_runtime_pool", None)
        if pool is None:
            from ..integration.provider_runtime import ProviderRuntimePool

            browser = getattr(self.session, "browser", self.session)
            pool = ProviderRuntimePool(browser)
            self._runtime_pool = pool
        return pool
