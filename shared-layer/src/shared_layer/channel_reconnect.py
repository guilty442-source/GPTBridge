"""A263 channel reconnect and heartbeat mixin (A185 split).

Contains the reconnect and _heartbeat_loop methods extracted from
A263Channel.
"""
from __future__ import annotations

import asyncio
import time

from .channel_types import ChannelGeneration, ChannelState


class ChannelReconnectMixin:
    """Reconnect and heartbeat loop operations."""

    config: object
    transport: object
    _generation: ChannelGeneration
    _generation_lock: object
    _heartbeat_dead: object
    _heartbeat_task: object
    _reconnect_attempts: int
    _reconnects: int
    _snapshot_cursor: int
    _snapshot_hash: str
    _snapshot_generation: ChannelGeneration
    _last_pong_received: float

    async def _set_state(self, state: ChannelState) -> None:
        raise NotImplementedError

    def _verify_snapshot(self, cursor: int, snapshot_hash: str) -> bool:
        raise NotImplementedError

    async def _send_resync(self, cursor: int) -> None:
        raise NotImplementedError

    async def _send_ping(self) -> None:
        raise NotImplementedError

    async def _receive_loop(self) -> None:
        raise NotImplementedError

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


__all__ = ["ChannelReconnectMixin"]
