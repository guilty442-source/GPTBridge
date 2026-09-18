"""Worker and handler mixin for GovernedToolRuntime (A185 split).

Contains the WebSocket request handler and the background queue
worker that claims, executes, and responds to governed requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from shared_layer.channel import SharedLayerChannel

from .sub_sovereign import ChannelHealth

from .governed_runtime_constants import (
    LOCAL_CLEANUP_COMMAND,
    GOVERNANCE_MAIN_ACTOR,
    permission_denied,
)

import websockets  # type: ignore

from websockets.exceptions import ConnectionClosed  # type: ignore


class GovernedRuntimeWorkerMixin:
    """Worker/handler methods for GovernedToolRuntime."""

    tool_id: str
    tool_root: Any
    channel: SharedLayerChannel
    _channels: dict[str, SharedLayerChannel]
    _processing_channel_ids: list[str]
    _channel_health: dict[str, ChannelHealth]
    waiters: dict[str, Any]
    shutdown_event: asyncio.Event
    executor: Any
    cancellation: Any
    _last_notification: Any
    _notified_request_ids: set[str]
    _notification_queue_size: int
    _start_time: float

    def _record_channel_health(self, channel_id: str, ok: bool) -> None:
        health = self._channel_health.get(channel_id)
        if health is not None:
            health.record(ok)

    async def send(self, websocket: Any, event: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        await websocket.send(message)

    async def emit(self, request_id: str, event: str, payload: dict[str, Any]) -> None:
        websocket = self.waiters.get(request_id)
        if websocket is not None:
            with contextlib.suppress(Exception):
                await self.send(websocket, event, payload)

    async def _listen_for_notifications(self, notify_queue: asyncio.Queue[str]) -> None:
        try:
            connection = self.channel.connect_listener()
        except Exception:
            return
        try:
            while not self.shutdown_event.is_set():
                try:
                    connection.poll(timeout=0.5)
                except Exception:
                    await asyncio.sleep(1.0)
                    continue
                while connection.notifies():
                    notify = connection.notifies.pop(0)
                    notify_queue.put_nowait(notify.payload)
        finally:
            with contextlib.suppress(Exception):
                connection.close()

    def _on_channel_notification(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            self._last_notification = {"raw": payload, "parsed": False}
            return
        request_id = str(data.get("request_id") or "")
        if request_id and request_id not in self._notified_request_ids:
            self._notified_request_ids.add(request_id)
        self._last_notification = {"request_id": request_id, "parsed": True}

    async def _worker(self) -> None:
        idle_poll_seconds = 0.25
        notify_queue: asyncio.Queue[str] = asyncio.Queue()
        listener_task: asyncio.Task[Any] | None = None
        try:
            listener_task = asyncio.create_task(
                self._listen_for_notifications(notify_queue)
            )
        except (RuntimeError, ValueError, TypeError):
            listener_task = None
        while not self.shutdown_event.is_set():
            request = None
            request_channel: SharedLayerChannel | None = None
            active_channel_id: str | None = None
            try:
                for channel_id in self._processing_channel_ids:
                    active_channel_id = channel_id
                    candidate_channel = self._channels[channel_id]
                    candidate = await asyncio.to_thread(candidate_channel.claim)
                    if candidate is not None:
                        request = candidate
                        request_channel = candidate_channel
                        break
            except Exception:
                # A transient denial (integrity re-anchor in flight, store
                # reconnect, auth rotation) must not kill the worker task —
                # a dead loop silently dead-letters the queue while the
                # process keeps serving websockets.  Back off and retry.
                if active_channel_id is not None:
                    self._record_channel_health(active_channel_id, ok=False)
                await asyncio.sleep(0.5)
                continue
            if request is None:
                wait_timeout = (
                    0.05
                    if not notify_queue.empty()
                    else max(idle_poll_seconds, 0.05)
                )
                try:
                    notification = await asyncio.wait_for(
                        notify_queue.get(), timeout=wait_timeout
                    )
                    self._on_channel_notification(notification)
                    idle_poll_seconds = 0.25
                    continue
                except asyncio.TimeoutError:
                    idle_poll_seconds = min(idle_poll_seconds * 1.5, 0.5)
                    continue
            idle_poll_seconds = 0.25
            request_id = str(request["request_id"])
            payload = request.get("payload")
            command = ""
            try:
                if not isinstance(payload, dict):
                    raise permission_denied()
                command = str(payload.pop("_governed_command", "")).strip()
                if not command:
                    raise permission_denied()
                payload["_governed_requester_actor"] = str(
                    request.get("requester_actor") or ""
                )
                if command == LOCAL_CLEANUP_COMMAND:
                    if payload["_governed_requester_actor"] != GOVERNANCE_MAIN_ACTOR:
                        raise permission_denied()
                    event = f"{LOCAL_CLEANUP_COMMAND}_result"
                    result = await self._run_local_cleanup()
                    if not isinstance(result, dict):
                        raise permission_denied()
                    result["request_id"] = request_id
                else:
                    execution_task = asyncio.create_task(
                        self.executor(command, payload, request_id)
                    )
                    cancelled_during_execution = False
                    while not execution_task.done():
                        done, _ = await asyncio.wait({execution_task}, timeout=0.1)
                        if done:
                            break
                        if request_channel is not None and await asyncio.to_thread(
                            request_channel.request_cancelled, request_id
                        ):
                            cancelled_during_execution = True
                            if self.cancellation is not None:
                                await self.cancellation(request_id)
                            execution_task.cancel()
                            await asyncio.gather(
                                execution_task, return_exceptions=True
                            )
                            self.waiters.pop(request_id, None)
                            break
                    if cancelled_during_execution:
                        continue
                    event, result = execution_task.result()
                    if not isinstance(result, dict):
                        raise permission_denied()
                    result["request_id"] = request_id
            except Exception:
                event = f"{command}_result" if command else "error"
                result = {
                    "ok": False,
                    "tool_id": self.tool_id,
                    "request_id": request_id,
                    "error_code": "PERMISSION_DENIED",
                    "message": "PERMISSION_DENIED",
                }
            try:
                if request_channel is None:
                    raise permission_denied()
                await asyncio.to_thread(request_channel.respond, request_id, result)
                if active_channel_id is not None:
                    self._record_channel_health(active_channel_id, ok=True)
            except Exception:
                event = f"{command}_result" if command else "error"
                if active_channel_id is not None:
                    self._record_channel_health(active_channel_id, ok=False)
                result = {
                    "ok": False,
                    "tool_id": self.tool_id,
                    "request_id": request_id,
                    "error_code": "PERMISSION_DENIED",
                    "message": "PERMISSION_DENIED",
                }
            websocket = self.waiters.pop(request_id, None)
            if websocket is not None:
                with contextlib.suppress(Exception):
                    await self.send(websocket, event, result)

    async def _handler(self, websocket: Any) -> None:
        try:
            await self._drain_messages(websocket)
        except ConnectionClosed:
            # Client disconnected mid-stream — a connection lifecycle event,
            # not a fault.  The sync message walker drains the waiters when
            # the socket closes, so nothing to diagnose or repair here.
            pass

    async def _drain_messages(self, websocket: Any) -> None:
        async for raw_message in websocket:
            command = ""
            request_id = ""
            try:
                message = json.loads(raw_message)
                command = str(message.get("command") or "").strip()
                payload = message.get("payload")
                if not command or not isinstance(payload, dict):
                    raise permission_denied()
                request_id = str(payload.get("request_id") or "").strip()
                if command == "toolbox_cancel_tool_run":
                    cancelled = bool(request_id) and await asyncio.to_thread(
                        self.channel.cancel, self.tool_id, request_id
                    )
                    if self.cancellation is not None and request_id:
                        cancelled = await self.cancellation(request_id) or cancelled
                    await self.send(
                        websocket,
                        "toolbox_cancel_tool_run_result",
                        {
                            "ok": cancelled,
                            "cancelled": cancelled,
                            "tool_id": self.tool_id,
                            "request_id": request_id,
                        },
                    )
                    continue
                if not request_id or len(request_id) > 256:
                    raise permission_denied()
                if str(payload.get("tool_id") or self.tool_id) != self.tool_id:
                    raise permission_denied()
                queued_payload = dict(payload)
                queued_payload["_governed_command"] = command
                self.waiters[request_id] = websocket
                await asyncio.to_thread(
                    self.channel.request,
                    self.tool_id,
                    request_id,
                    queued_payload,
                )
                await self.send(
                    websocket,
                    "COMMAND_RECEIVED",
                    {"command": command, "status": "processing"},
                )
            except Exception:
                await self.send(
                    websocket,
                    f"{command}_result" if command else "error",
                    {
                        "ok": False,
                        "tool_id": self.tool_id,
                        "request_id": request_id,
                        "error_code": "PERMISSION_DENIED",
                        "message": "PERMISSION_DENIED",
                    },
                )


__all__ = ["GovernedRuntimeWorkerMixin"]
