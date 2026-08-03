from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from governance_rule.permission_directory.execution.path_guard import permission_denied

from .channel import SharedLayerChannel


class GovernedRequestClient:
    """Business-neutral request/response helper over the governed shared layer."""

    def __init__(
        self,
        channel: SharedLayerChannel,
        caller_actor: str,
        authorize_route: Callable[[str, str, str], Any],
        *,
        transport: str = "governed-shared-layer",
    ) -> None:
        if not isinstance(channel, SharedLayerChannel):
            raise permission_denied()
        actor = str(caller_actor or "").strip()
        if not actor or not callable(authorize_route):
            raise permission_denied()
        self._channel = channel
        self._caller_actor = actor
        self._authorize_route = authorize_route
        self._transport = str(transport or "governed-shared-layer").strip()

    @property
    def configured(self) -> bool:
        return True

    def request_sync(
        self,
        target_tool_id: str,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 90,
        request_id: str | None = None,
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize_route(self._caller_actor, target_tool_id, command)
        if not isinstance(payload, dict):
            raise permission_denied()
        request_id = str(request_id or f"request-{uuid.uuid4().hex}").strip()
        self._channel.request(
            target_tool_id,
            request_id,
            {**dict(payload), "_governed_command": command},
        )
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        last_progress_sequence = -1
        while time.monotonic() < deadline:
            state = self._channel.response(target_tool_id, request_id)
            progress = state.get("progress") if isinstance(state, dict) else None
            if isinstance(progress, dict) and progress_callback is not None:
                sequence = int(progress.get("sequence") or 0)
                if sequence > last_progress_sequence:
                    last_progress_sequence = sequence
                    try:
                        progress_callback(dict(progress))
                    except Exception:
                        # Progress is observational; it must not abort the
                        # governed request or its final response.
                        pass
            if state is not None and state.get("status") == "completed":
                response = state.get("response")
                if isinstance(response, dict):
                    response.pop("request_id", None)
                    return {
                        **response,
                        "queued": False,
                        "transport": self._transport,
                    }
                raise permission_denied()
            if state is not None and state.get("status") == "cancelled":
                return {
                    "ok": False,
                    "queued": False,
                    "transport": self._transport,
                    "error_code": "GOVERNED_REQUEST_CANCELLED",
                    "message": "Governed request was cancelled",
                }
            time.sleep(0.05)
        self._channel.cancel(target_tool_id, request_id)
        return {
            "ok": False,
            "queued": False,
            "transport": self._transport,
            "error_code": "GOVERNED_REQUEST_TIMEOUT",
            "message": "Governed request timed out",
        }

    async def request(
        self,
        target_tool_id: str,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 90,
        request_id: str | None = None,
        progress_callback: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.request_sync,
            target_tool_id,
            command,
            payload,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
            progress_callback=progress_callback,
        )

    def cancel(self, target_tool_id: str, request_id: str) -> bool:
        return self._channel.cancel(target_tool_id, request_id)
