from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


RouteHandler = Callable[[str, dict[str, Any]], Awaitable[tuple[str, dict[str, Any]]]]
AuditSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class GovernedCommandEnvelope:
    sender: str
    destination: str
    command: str
    payload: dict[str, Any]


class InformationChannelGateway:
    """Typed, observable queue between transports and runtime destinations."""

    def __init__(self, handler: RouteHandler, audit: AuditSink | None = None) -> None:
        if not callable(handler):
            raise TypeError("route handler is required")
        self._handler = handler
        self._audit = audit
        self._queue: asyncio.Queue[
            tuple[GovernedCommandEnvelope, asyncio.Future[tuple[str, dict[str, Any]]]]
        ] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def dispatch(
        self,
        *,
        sender: str,
        destination: str,
        command: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        envelope = self._validate(sender, destination, command, payload)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, dict[str, Any]]] = loop.create_future()
        await self._queue.put((envelope, future))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="information-channel")
        return await future

    @staticmethod
    def _validate(
        sender: str,
        destination: str,
        command: str,
        payload: dict[str, Any],
    ) -> GovernedCommandEnvelope:
        sender = str(sender or "").strip()
        destination = str(destination or "").strip()
        command = str(command or "").strip()
        if not sender or not destination or not command or not isinstance(payload, dict):
            raise PermissionError("INVALID_INFORMATION_CHANNEL_ENVELOPE")
        return GovernedCommandEnvelope(sender, destination, command, dict(payload))

    async def _run(self) -> None:
        while not self._queue.empty():
            envelope, future = await self._queue.get()
            try:
                if self._audit is not None:
                    self._audit(
                        {
                            "transport_owner": "shared-layer",
                            "channel": "system",
                            "sender": envelope.sender,
                            "destination": envelope.destination,
                            "command": envelope.command,
                        }
                    )
                result = await self._handler(envelope.command, envelope.payload)
                if not future.done():
                    future.set_result(result)
            except Exception as error:
                if not future.done():
                    future.set_exception(error)
            finally:
                self._queue.task_done()


__all__ = ["GovernedCommandEnvelope", "InformationChannelGateway"]
