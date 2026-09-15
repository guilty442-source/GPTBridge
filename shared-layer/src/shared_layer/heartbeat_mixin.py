"""Heartbeat Mixin — A263 two-way heartbeat with deadline.

Provides heartbeat functionality for A263-compliant channels.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from .channel_types import ChannelConfig


class HeartbeatMixin:
    """Mixin providing two-way heartbeat with deadline (A263)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_dead = asyncio.Event()
        self._last_ping_sent: float = 0.0
        self._last_pong_received: float = 0.0
        self._heartbeats_sent: int = 0
        self._heartbeats_received: int = 0

    @property
    def config(self) -> ChannelConfig:
        if getattr(self, "_config", None) is None:
            raise NotImplementedError("Subclass must configure the channel")
        return self._config

    @property
    def transport(self):
        if getattr(self, "_config", None) is None:
            raise NotImplementedError("Subclass must configure the transport")
        return getattr(self, "_transport", None)

    @property
    def generation(self):
        raise NotImplementedError("Subclass must implement 'generation' property")

    async def _heartbeat_loop(self) -> None:
        """Two-way heartbeat with deadline (A263)."""
        while not self._heartbeat_dead.is_set():
            await asyncio.sleep(self.config.heartbeat_interval_seconds)
            if self._heartbeat_dead.is_set():
                break

            try:
                await self._send_ping()
            except Exception:
                self._heartbeat_dead.set()
                break

            # Check deadline
            if (
                time.monotonic() - self._last_pong_received
                > self.config.heartbeat_timeout_seconds
            ):
                self._heartbeat_dead.set()
                try:
                    await self.transport.close(1001, "heartbeat_timeout")
                except Exception:
                    pass
                break

    async def _send_ping(self) -> None:
        """Send heartbeat ping."""
        from datetime import datetime, timezone
        message = {
            "type": "control",
            "command": "heartbeat_ping",
            "payload": {"t": datetime.now(timezone.utc).isoformat()},
            "generation": self.generation.as_dict(),
        }
        await self._enqueue_control(message)
        self._last_ping_sent = time.monotonic()
        self._heartbeats_sent += 1

    async def _handle_pong(self, payload: dict[str, Any]) -> None:
        """Handle heartbeat pong - updates deadline."""
        self._last_pong_received = time.monotonic()
        self._heartbeats_received += 1


__all__ = ["HeartbeatMixin"]