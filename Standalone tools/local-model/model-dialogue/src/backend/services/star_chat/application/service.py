from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .programming_tools import collect_programming_context, render_programming_context
from .service_helpers import StarChatHelpersMixin


def _service_version() -> str:
    try:
        from core_system.versioning import component_version

        return component_version("model-dialogue")
    except Exception:
        pass
    manifest_path = Path(__file__).resolve().parents[5] / "manifest.json"
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"


class StarChatService(StarChatHelpersMixin):
    """A separated, governed client for local model conversation."""

    VERSION = _service_version()
    COMMANDS = frozenset(
        {
            "star_chat_status",
            "star_chat_send_message",
            "star_chat_codex_alignment",
            "star_chat_architecture_sync",
            "star_chat_get_persona",
            "star_chat_save_persona",
        }
    )
    def __init__(self) -> None:
        self._client: Any | None = None
        self._local_service: Any | None = None
        self._active_nested_requests: dict[str, str] = {}
        self._target_ready_state = False
        self._target_ready_until = 0.0

    #: Liveness probe: a model-owner round trip must answer within this bound.
    #: The probe itself queues behind the owner's serial worker, so a short
    #: timeout avoids adding seconds of dead time before every send while an
    #: inference is still in flight; a queued infer request is what triggers
    #: governed lazy activation anyway.
    _PROBE_TIMEOUT_SECONDS = 1.2
    _AVAILABLE_TTL_SECONDS = 120.0
    _UNAVAILABLE_TTL_SECONDS = 3.0
    #: When the model owner is being lazily activated, the infer request may
    #: wait for the governed on-demand start plus model warm-up.
    _ACTIVATION_TIMEOUT_SECONDS = 240.0

    def bind_channel(self, channel: Any) -> None:
        from governance_rule.permission_directory.registries.permissions.tool_routes import (
            authorize_ai_route,
            tool_actor,
        )
        from shared_layer import GovernedRequestClient

        self._client = GovernedRequestClient(
            channel,
            tool_actor("model-dialogue"),
            authorize_ai_route,
            transport="governance-authenticated-ai-channel",
        )

    def bind_local_service(self, service: Any) -> None:
        """Bind the dialogue facade directly to its physical owner runtime."""

        self._local_service = service

    @property
    def connected(self) -> bool:
        return self._client is not None or self._local_service is not None

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def _unavailable_result(self) -> dict[str, Any]:
        """Typed answer when the xingcheng runtime is not running.

        The governed AI channel is asynchronous: without this fast path an
        infer request would sit queued until its 600 s deadline and the UI
        would show no response at all.
        """
        return {
            "ok": False,
            "queued": False,
            "error_code": "AI_CHANNEL_NOT_CONNECTED",
            "message": "星澄模型服務未啟動，請先啟動「本機模型」工具後再試。",
        }

    async def _target_ready(self) -> bool:
        """Cheap, cached liveness probe for the xingcheng runtime."""
        if self._local_service is not None:
            return True
        if self._client is None:
            return False
        now = time.monotonic()
        if now < self._target_ready_until:
            return self._target_ready_state
        try:
            probe = await self._client.request(
                "xingcheng",
                "xingcheng_status",
                {},
                timeout_seconds=self._PROBE_TIMEOUT_SECONDS,
            )
            available = not (
                probe.get("ok") is False
                and str(probe.get("error_code") or "")
                in {"GOVERNED_REQUEST_TIMEOUT", "GOVERNED_REQUEST_CANCELLED"}
            )
        except PermissionError:
            raise
        except Exception:
            available = False
        self._target_ready_state = available
        self._target_ready_until = now + (
            self._AVAILABLE_TTL_SECONDS
            if available
            else self._UNAVAILABLE_TTL_SECONDS
        )
        return available

    async def _request(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        parent_request_id: str = "",
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
        cancel_event: Any | None = None,
    ) -> dict[str, Any]:
        if self._local_service is not None:
            return await self._request_via_local(
                command, payload, progress_callback, cancel_event
            )
        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄 AI 通道尚未連線。",
            }
        return await self._request_via_client(
            command, payload, timeout_seconds, parent_request_id,
            progress_callback,
        )

    async def _request_via_local(
        self,
        command: str,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], Any] | None,
        cancel_event: Any | None,
    ) -> dict[str, Any]:
        local_payload = dict(payload)
        if progress_callback is not None:
            local_payload["_progress_callback"] = progress_callback
        if cancel_event is not None:
            local_payload["_cancel_event"] = cancel_event
        _event, result = await self._local_service.handle(command, local_payload)
        if not isinstance(result, dict):
            raise PermissionError("PERMISSION_DENIED")
        return result

    async def _request_via_client(
        self,
        command: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        parent_request_id: str,
        progress_callback: Callable[[dict[str, Any]], Any] | None,
    ) -> dict[str, Any]:
        nested_request_id = (
            f"star-nested-{uuid.uuid4().hex}" if parent_request_id else None
        )
        if parent_request_id and nested_request_id:
            self._active_nested_requests[parent_request_id] = nested_request_id
        try:
            if nested_request_id:
                return await self._client.request(
                    "xingcheng",
                    command,
                    payload,
                    timeout_seconds=timeout_seconds,
                    request_id=nested_request_id,
                    progress_callback=progress_callback,
                )
            return await self._client.request(
                "xingcheng", command, payload, timeout_seconds=timeout_seconds
            )
        finally:
            if (
                parent_request_id
                and self._active_nested_requests.get(parent_request_id)
                == nested_request_id
            ):
                self._active_nested_requests.pop(parent_request_id, None)

    async def cancel_request(self, parent_request_id: str) -> bool:
        if self._local_service is not None:
            return bool(await self._local_service.cancel_request(parent_request_id))
        nested_request_id = self._active_nested_requests.get(
            str(parent_request_id or "").strip()
        )
        if self._client is None or not nested_request_id:
            return False
        return await asyncio.to_thread(
            self._client.cancel, "xingcheng", nested_request_id
        )

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        request_id: str = "",
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if command == "star_chat_status":
            return await self._handle_status(payload, request_id, command)
        if command == "star_chat_send_message":
            return await self._handle_send_message(
                payload, request_id, progress_callback, command
            )
        if command == "star_chat_codex_alignment":
            return await self._handle_codex_diagnostic(
                request_id, command, "xingcheng_codex_alignment"
            )
        if command == "star_chat_architecture_sync":
            return await self._handle_codex_diagnostic(
                request_id, command, "xingcheng_codex_mirror_check"
            )
        if command == "star_chat_get_persona":
            return await self._handle_get_persona(payload, request_id, command)
        if command == "star_chat_save_persona":
            return await self._handle_save_persona(payload, request_id, command)
        raise PermissionError("PERMISSION_DENIED")

    async def _handle_codex_diagnostic(
        self, request_id: str, command: str, target_command: str
    ) -> tuple[str, dict[str, Any]]:
        """Forward a read-only codex diagnostic to the model owner."""
        if not await self._target_ready():
            return f"{command}_result", {
                **self._unavailable_result(),
                "channel_workflow": self._channel_workflow(),
            }
        result = await self._request(
            target_command,
            {},
            timeout_seconds=300,
            parent_request_id=request_id,
        )
        summary = self._codex_diagnostic_text(command, result)
        return f"{command}_result", {**result, "response": summary, "summary": summary}

    def _codex_diagnostic_text(
        self, command: str, result: dict[str, Any]
    ) -> str:
        """Human-readable zh-TW rendering of one diagnostic report."""
        title = (
            "法典 × 實作對齊檢查"
            if command == "star_chat_codex_alignment"
            else "中文法典 × 架構圖同步與完整性檢查"
        )
        lines = [f"【{title}】"]
        if result.get("ok") is not True:
            detail = str(result.get("message") or result.get("error_code") or "")
            if detail:
                lines.append(f"檢查未完成：{detail}")
            else:
                lines.append("檢查未完成。")
        lines.append(f"結果：{'通過' if result.get('ok') is True else '發現問題'}")
        if "complete" in result:
            lines.append(
                "完整性：" + ("完整" if result.get("complete") else "尚未完整")
            )
        mirror = result.get("mirror")
        if isinstance(mirror, dict) and mirror:
            lines.append(
                "鏡像版本："
                f"{mirror.get('codex_version', '')}"
                f"（5 段鏈結、{mirror.get('tables', 0)} 表 / {mirror.get('rows', 0)} 列）"
            )
        documents = result.get("architecture_documents")
        if isinstance(documents, dict) and documents:
            lines.append(
                "架構圖文件："
                f"{documents.get('document_count', 0)} 份，"
                f"覆蓋 {documents.get('canonical_components', 0)} 個標準組件"
            )
        errors = [str(item) for item in (result.get("errors") or ())]
        if errors:
            lines.append(f"問題 {len(errors)} 項：")
            lines.extend(f"- {item}" for item in errors[:15])
            if len(errors) > 15:
                lines.append(f"- …另有 {len(errors) - 15} 項")
        gaps = []
        if isinstance(documents, dict):
            gaps = [item for item in (documents.get("gaps") or ())]
        if gaps:
            lines.append(f"架構圖缺口 {len(gaps)} 項：")
            for item in gaps[:15]:
                if isinstance(item, dict):
                    lines.append(
                        f"- {item.get('component_id', '')}：{item.get('reason', '')}"
                    )
                else:
                    lines.append(f"- {item}")
            if len(gaps) > 15:
                lines.append(f"- …另有 {len(gaps) - 15} 項")
        if result.get("ok") is True and not gaps:
            lines.append("未發現不一致。")
        return "\n".join(lines)

    async def _handle_status(
        self, payload: dict[str, Any], request_id: str, command: str
    ) -> tuple[str, dict[str, Any]]:
        if await self._target_ready():
            result = await self._request(
                "xingcheng_status",
                {},
                timeout_seconds=120,
                parent_request_id=request_id,
                cancel_event=payload.get("_cancel_event"),
            )
        else:
            result = self._unavailable_result()
        return f"{command}_result", {
            **result,
            "client_version": self.VERSION,
            "client_tool": "model-dialogue",
            "main_system_independent_tool": True,
            "independent_only_in": "main-system",
            "model_service_owner": "xingcheng",
            "settings_owner": "model-dialogue",
            "business_layer_owner": "model-dialogue",
            "permission_profile": "local-model-platform-v1",
            "cache_owner": "model-dialogue",
            "cache_storage": "model-dialogue/runtime/cache",
            "backup_owner": "model-dialogue",
            "backup_storage": "global-cleaner/data/business/backups/model-dialogue",
            "separated_from_model_service": True,
            "database_shared": True,
            "separate_business_layer": False,
            "separate_settings_layer": False,
            "channel_workflow": self._channel_workflow(),
            "programming_tools": {
                "enabled": True,
                "mode": "automatic-governed-read-only",
                "available": ["workspace_list", "workspace_read"],
                "scope": "user-selected-programming-folder",
            },
        }

    async def _handle_send_message(
        self,
        payload: dict[str, Any],
        request_id: str,
        progress_callback: Callable[[dict[str, Any]], Any] | None,
        command: str,
    ) -> tuple[str, dict[str, Any]]:
        raw_message = self._bounded_text(
            payload.get("message") or payload.get("prompt"), 32_000,
        )
        command_context = self._conversation_context(payload)
        programming_tool_result: dict[str, Any] = {}
        has_programming_scope = bool(
            self._bounded_text(payload.get("programming_folder"), 1_024)
        )
        if has_programming_scope:
            command_context, payload, programming_tool_result = (
                await self._collect_coding_context(
                    payload, raw_message, command_context
                )
            )
        prompt = self._conversation_prompt(payload)
        if command_context and has_programming_scope:
            prompt = f"{prompt}\n\n{command_context}"
        if not prompt:
            return f"{command}_result", {
                "ok": False,
                "error_code": "MESSAGE_REQUIRED",
                "message": "請輸入要對星澄說的內容。",
            }
        controls = self._resolve_generation_controls(payload)
        target_ready = await self._target_ready()
        if not target_ready and self._client is None:
            return f"{command}_result", {
                **self._unavailable_result(),
                "channel_workflow": self._channel_workflow(),
            }
        if not target_ready and progress_callback is not None:
            try:
                progress_callback({
                    "phase": "activating-model",
                    "message": "正在啟動星澄模型服務…",
                })
            except Exception:
                # Progress is observational; it must not abort the request.
                pass
        result = await self._request(
            "xingcheng_infer",
            self._infer_payload(
                payload, prompt, raw_message, command_context, controls,
            ),
            timeout_seconds=(
                600.0 if target_ready else self._ACTIVATION_TIMEOUT_SECONDS
            ),
            parent_request_id=request_id,
            progress_callback=progress_callback,
            cancel_event=payload.get("_cancel_event"),
        )
        if (
            not target_ready
            and result.get("ok") is False
            and str(result.get("error_code") or "")
            in {"GOVERNED_REQUEST_TIMEOUT", "GOVERNED_REQUEST_CANCELLED"}
        ):
            result = {
                **result,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄模型服務啟動逾時，請稍後再試，或先啟動「本機模型」工具。",
            }
        result["channel_workflow"] = self._channel_workflow()
        if has_programming_scope:
            result["programming_tools"] = programming_tool_result
        return f"{command}_result", result

    async def _handle_get_persona(
        self, payload: dict[str, Any], request_id: str, command: str
    ) -> tuple[str, dict[str, Any]]:
        if not await self._target_ready():
            return f"{command}_result", {
                **self._unavailable_result(),
                "persona_text": "",
            }
        result = await self._request(
            "xingcheng_sql_get_personality",
            {},
            timeout_seconds=30,
            parent_request_id=request_id,
            cancel_event=payload.get("_cancel_event"),
        )
        record = result if isinstance(result, dict) else {}
        value = record.get("value") if isinstance(record.get("value"), dict) else {}
        return f"{command}_result", {
            "ok": record.get("ok", True) is not False,
            "persona_text": self._bounded_text(value.get("persona_text"), 4_000),
            "persona_version": record.get("version"),
        }

    async def _handle_save_persona(
        self, payload: dict[str, Any], request_id: str, command: str
    ) -> tuple[str, dict[str, Any]]:
        persona_text = self._bounded_text(payload.get("persona_text"), 4_000)
        if not await self._target_ready():
            return f"{command}_result", self._unavailable_result()
        result = await self._request(
            "xingcheng_sql_save_personality",
            {
                "personality": {
                    "persona_text": persona_text,
                    "source": "model-dialogue",
                },
                # The UI save button is the explicit user confirmation.
                "confirmed": True,
            },
            timeout_seconds=30,
            parent_request_id=request_id,
            cancel_event=payload.get("_cancel_event"),
        )
        if not isinstance(result, dict):
            result = {"ok": False, "message": "人格儲存失敗"}
        return f"{command}_result", result

    async def _collect_coding_context(
        self,
        payload: dict[str, Any],
        raw_message: str,
        command_context: str,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        programming_tool_result = await asyncio.to_thread(
            collect_programming_context,
            self._bounded_text(payload.get("programming_folder"), 1_024),
            raw_message,
        )
        tool_context = render_programming_context(programming_tool_result)
        if tool_context:
            command_context = "\n\n".join(
                part for part in (command_context, tool_context) if part
            )
            payload = {**payload, "history": [], "_tool_context": tool_context}
        return command_context, payload, programming_tool_result


__all__ = ["StarChatService"]
