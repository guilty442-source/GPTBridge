"""Connection Mixin — A263 connection and reconnection management.

Provides connection lifecycle for A263-compliant channels.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from .channel_types import ChannelConfig, ChannelGeneration, ChannelState
from datetime import datetime, timezone


class ConnectionMixin:
    """Mixin providing connection and reconnection management (A263)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._state = ChannelState.CLOSED
        self._generation = ChannelGeneration(
            channel_id="",
            generation=0,
        )
        if not hasattr(self, "_config"):
            self._config: ChannelConfig | None = None
        if not hasattr(self, "_transport"):
            self._transport: object | None = None
        self._generation_lock = asyncio.Lock()
        self._reconnect_attempts: int = 0
        self._reconnect_task: Optional[asyncio.Task] = None
        self._snapshot_hash: str = ""
        self._snapshot_cursor: int = 0
        self._snapshot_generation: ChannelGeneration | None = None

    @property
    def config(self) -> ChannelConfig:
        if self._config is None:
            raise NotImplementedError("Subclass must configure the channel")
        return self._config

    @config.setter
    def config(self, value: ChannelConfig) -> None:
        self._config = value

    @property
    def transport(self):
        if self._transport is None:
            raise NotImplementedError("Subclass must configure the transport")
        return self._transport

    @transport.setter
    def transport(self, value) -> None:
        self._transport = value

    @property
    def generation(self) -> ChannelGeneration:
        return self._generation

    @property
    def state(self) -> ChannelState:
        return self._state

    async def _set_state(self, new_state: ChannelState) -> None:
        old_state = self._state
        if old_state == new_state:
            return
        self._state = new_state
        if self._on_state_change:
            try:
                await self._on_state_change(old_state, new_state)
            except Exception:
                pass

    def set_callbacks(
        self,
        on_state_change: Optional[callable] = None,
        on_message: Optional[callable] = None,
        on_control: Optional[callable] = None,
    ) -> None:
        self._on_state_change = on_state_change
        self._on_message = on_message
        self._on_control = on_control

    async def connect(self, backend_generation: str = "", session_id: str = "") -> ChannelGeneration:
        """Establish channel connection with new generation."""
        async with self._generation_lock:
            self._generation = ChannelGeneration(
                channel_id=self.config.channel_id,
                generation=self._generation.generation + 1,
                backend_generation=backend_generation,
                session_id=session_id,
            )
            self._reconnect_attempts = 0

        await self._set_state(ChannelState.CONNECTING)
        self._heartbeat_dead.clear()

        # Start heartbeat monitor
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Start receive loop
        asyncio.create_task(self._receive_loop())

        # Start send loop (bidirectional flow)
        if getattr(self, "_start_send_loop", None) is not None:
            self._start_send_loop()

        # Send hello with cursor for reconnection
        await self._send_hello()

        await self._set_state(ChannelState.OPEN)
        return self._generation

    async def disconnect(self, code: int = 1000, reason: str = "") -> None:
        """Graceful disconnect - invalidates ready (A263)."""
        self._heartbeat_dead.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        if getattr(self, "_send_task", None) is not None:
            self._send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._send_task
        if self._reconnect_task:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task

        await self.transport.close(code, reason)
        await self._set_state(ChannelState.CLOSED)

    async def reconnect(
        self,
        snapshot_cursor: int,
        snapshot_hash: str,
        backend_generation: str = "",
        session_id: str = "",
    ) -> ChannelGeneration:
        """Reconnect with snapshot/cursor/hash convergence (A263).

        Requires:
        - Snapshot cursor (last acknowledged sequence)
        - Snapshot hash (state integrity verification)
        - Backend generation (for generation tracking)
        """
        # Verify snapshot integrity
        if not self._verify_snapshot(snapshot_cursor, snapshot_hash):
            raise ValueError("snapshot integrity verification failed")

        # Save snapshot for convergence
        self._snapshot_cursor = snapshot_cursor
        self._snapshot_hash = snapshot_hash
        self._snapshot_generation = self._generation

        await self._set_state(ChannelState.RECONNECTING)
        self._heartbeat_dead.clear()
        self._reconnect_attempts += 1

        if self._reconnect_attempts > self.config.reconnect_max_attempts:
            await self._set_state(ChannelState.DEAD)
            raise ConnectionError("max reconnect attempts exceeded")

        # Attempt reconnection
        await self.transport.close(1001, "reconnect")

        # Exponential backoff
        delay = self.config.reconnect_base_delay_seconds * (2 ** (self._reconnect_attempts - 1))
        await asyncio.sleep(delay)

        # New generation for reconnection
        async with self._generation_lock:
            self._generation = ChannelGeneration(
                channel_id=self.config.channel_id,
                generation=self._generation.generation + 1,
                backend_generation=backend_generation,
                session_id=session_id,
            )

        # Re-establish transport (transport-specific)
        # This would be implemented by the transport adapter

        self._heartbeat_dead.clear()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._receive_loop())

        # Restart send loop (bidirectional flow)
        if getattr(self, "_start_send_loop", None) is not None:
            self._start_send_loop()

        # Send resync with cursor
        await self._send_resync(snapshot_cursor)

        await self._set_state(ChannelState.OPEN)
        self._reconnects += 1
        return self._generation

    def _verify_snapshot(self, cursor: int, snapshot_hash: str) -> bool:
        """Verify snapshot/cursor/hash convergence (A263)."""
        # In a full implementation, this would verify the hash against stored state
        # For now, accept if cursor is valid
        return cursor >= 0


__all__ = ["ConnectionMixin"]