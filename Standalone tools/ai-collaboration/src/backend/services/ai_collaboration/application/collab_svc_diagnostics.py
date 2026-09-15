from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class CollabSvcDiagnosticsMixin:
    """Diagnostics and report-export handlers for AiCollaborationService."""

    def _diagnostics(
        self,
        agents: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        memory_items: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_agents = [agent for agent in agents if agent.get("selected")]
        enabled_agents = [agent for agent in agents if agent.get("enabled")]
        status_counts = self._agent_status_counts(agents)
        (
            failed_responses,
            waiting_verification,
            completed_responses,
            latest_message,
        ) = self._response_tallies(messages)
        state, message = self._diagnostics_state(
            failed_responses,
            waiting_verification,
            status_counts,
            selected_agents,
            enabled_agents,
        )
        browser_status = self._browser_status()
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
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _agent_status_counts(
        agents: list[dict[str, Any]],
    ) -> dict[str, int]:
        status_counts: dict[str, int] = {}
        for agent in agents:
            status = str(agent.get("status") or "idle")
            status_counts[status] = status_counts.get(status, 0) + 1
        return status_counts

    @staticmethod
    def _response_tallies(
        messages: list[dict[str, Any]],
    ) -> tuple[int, int, int, dict[str, Any] | None]:
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
        return failed_responses, waiting_verification, completed_responses, latest_message

    @staticmethod
    def _diagnostics_state(
        failed_responses: int,
        waiting_verification: int,
        status_counts: dict[str, int],
        selected_agents: list[dict[str, Any]],
        enabled_agents: list[dict[str, Any]],
    ) -> tuple[str, str]:
        if failed_responses or waiting_verification:
            return "attention", "有外部 AI 需要檢查登入、驗證或回覆錯誤。"
        if status_counts.get("running"):
            return "running", "AI 協作正在執行。"
        if not selected_agents:
            return "setup", "尚未選擇要協作的 AI。"
        if enabled_agents:
            return "ready", "AI 協作工具已就緒。"
        return "empty", "沒有可用 AI。"

    def _browser_status(self) -> dict[str, Any]:
        if hasattr(self.session, "browser_status"):
            return self.session.browser_status()
        return {
            "product": "embedded-browser-view",
            "available": False,
            "mode": "embedded-browser-view",
            "automation": False,
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
