from __future__ import annotations

import json
from datetime import datetime
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
                "product": "embedded-browser-view",
                "available": False,
                "mode": "embedded-browser-view",
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
