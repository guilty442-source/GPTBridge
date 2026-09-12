from __future__ import annotations

from typing import Any


class CollabSvcMemoryMixin:
    """Memory, task, and auto-summary handlers for AiCollaborationService."""

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
