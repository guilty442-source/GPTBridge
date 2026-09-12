from __future__ import annotations
import asyncio
import json
from typing import Any, Dict, Protocol
import websockets


# Bound every outbound frame: a backpressured or half-dead socket must not
# stall the shared push loops (status push, state-change notifier, outbox
# drain, heartbeat monitor).  The timeout propagates as TimeoutError so
# callers' dead-shell detection discards the stalled connection.
SEND_TIMEOUT_SECONDS = 2.0


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> Any: ...

class UIShell:
    def __init__(self, websocket: WebSocketConnection) -> None:
        self.websocket = websocket

    async def _send(self, message: str) -> None:
        try:
            await asyncio.wait_for(
                self.websocket.send(message), timeout=SEND_TIMEOUT_SECONDS
            )
        except (websockets.exceptions.ConnectionClosed, RuntimeError):
            return

    async def send_event(self, event: str, payload: Dict[str, Any]) -> None:
        message = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        await self._send(message)

    async def send_log(self, message: str) -> None:
        await self._send(json.dumps({"type": "LOG", "message": message}, ensure_ascii=False))

    async def send_error(self, message: str, context: str | None = None) -> None:
        payload: Dict[str, Any] = {"message": message}
        if context is not None:
            payload["context"] = context
        await self.send_event("error", payload)
