from __future__ import annotations
import json
from typing import Any, Dict
import websockets
from websockets import WebSocketServerProtocol

class UIShell:
    def __init__(self, websocket: WebSocketServerProtocol) -> None:
        self.websocket = websocket

    async def _send(self, message: str) -> None:
        try:
            await self.websocket.send(message)
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