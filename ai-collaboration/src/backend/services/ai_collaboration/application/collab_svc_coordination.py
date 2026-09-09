from __future__ import annotations

import json
from typing import Any


class CollabSvcCoordinationMixin:
    """Coordination, pipeline, and business-scope helpers for AiCollaborationService."""

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
