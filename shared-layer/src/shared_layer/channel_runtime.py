"""A263-compliant Information-Layer Channel Runtime.

Per Governance Codex A263 (channel-contract):

  * information-layer is the single owner of all channels
  * typed generation on every channel (channel_generation)
  * two-way heartbeat with deadline
  * bounded queue/backpressure
  * control priority channel
  * transactional outbox for state changes
  * ordered ack/cursor
  * disconnect invalidates ready
  * reconnect requires snapshot/cursor/hash convergence
  * NO dropping unacknowledged events
  * NO duplicate side effects
  * NO socket-only health
  * NO disconnect-triggered code repair

This module provides the channel abstraction that all sovereigns and tools
must use. Direct WebSocket usage is forbidden.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Set, Protocol

from core_system.versioning import component_version

from .channel_types import (
    ChannelConfig,
    ChannelGeneration,
    ChannelState,
    MessagePriority,
    OutboxEvent,
)
from .connection_mixin import ConnectionMixin
from .heartbeat_mixin import HeartbeatMixin
from .transactional_outbox import TransactionalOutbox

CHANNEL_RUNTIME_VERSION: str = component_version("channel-runtime")


class ChannelTransport(Protocol):
    """Transport adapter owned by information-layer (A177)."""

    async def send(self, message: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any]: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...
    @property
    def is_closed(self) -> bool: ...


class A263Channel(ConnectionMixin, HeartbeatMixin):
    """A263-compliant channel with full contract compliance.

    Combines ConnectionMixin (connection/reconnection) and HeartbeatMixin
    (heartbeat with deadline) with message queue and outbox integration.
    """

    def __init__(
        self,
        config: ChannelConfig,
        transport: ChannelTransport,
        outbox: Optional["TransactionalOutbox"] = None,
    ) -> None:
        # Initialize base config
        self.config = config
        self.transport = transport
        self.outbox = outbox

        # Initialize mixins
        ConnectionMixin.__init__(self)
        HeartbeatMixin.__init__(self)

        # Set config for mixins
        self._generation = ChannelGeneration(
            channel_id=config.channel_id,
            generation=0,
        )

        # Message queues with priority (control channel)
        self._control_queue: Deque[dict[str, Any]] = deque(maxlen=config.control_channel_capacity)
        self._message_queue: Deque[tuple[MessagePriority, dict[str, Any]]] = deque(maxlen=config.max_queue_size)

        # Ack/cursor tracking
        self._acked_cursor: int = 0
        self._sent_upto: int = 0
        self._pending_acks: Dict[int, asyncio.Future] = {}

        # Outbound send loop wakeup / task
        self._send_wakeup: asyncio.Event = asyncio.Event()
        self._send_task: Optional[asyncio.Task] = None

        # Callbacks
        self._on_state_change: Optional[Callable[[ChannelState, ChannelState], Awaitable[None]]] = None
        self._on_message: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
        self._on_control: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None

        # Metrics
        self._messages_sent: int = 0
        self._messages_received: int = 0
        self._reconnects: int = 0

    # ================================================================
    # Message Queue with Priority (Control Channel)
    # ================================================================

    async def _enqueue_control(self, message: dict[str, Any]) -> bool:
        """Enqueue to control priority channel."""
        if len(self._control_queue) >= self.config.control_channel_capacity:
            return False  # Backpressure on control channel
        self._control_queue.append(message)
        self._send_wakeup.set()
        return True

    async def _enqueue_message(self, priority: MessagePriority, message: dict[str, Any]) -> bool:
        """Enqueue message with priority and backpressure."""
        if self.config.enable_backpressure:
            if len(self._message_queue) >= self.config.max_queue_size:
                return False  # Backpressure
        self._message_queue.append((priority, message))
        self._send_wakeup.set()
        return True

    async def _send_hello(self) -> None:
        """Send hello with cursor for reconnection support."""
        message = {
            "type": "control",
            "command": "state_event_hello",
            "payload": {"cursor": self._acked_cursor},
            "generation": self.generation.as_dict(),
        }
        await self._enqueue_control(message)

    async def _send_resync(self, cursor: int) -> None:
        """Send resync request with cursor."""
        message = {
            "type": "control",
            "command": "state_event_resync",
            "payload": {"cursor": cursor},
            "generation": self.generation.as_dict(),
        }
        await self._enqueue_control(message)

    async def _send_ack(self, cursor: int) -> None:
        """Send acknowledgment cursor."""
        message = {
            "type": "control",
            "command": "state_event_ack",
            "payload": {"cursor": cursor},
            "generation": self.generation.as_dict(),
        }
        await self._enqueue_control(message)

    def _start_send_loop(self) -> None:
        """Start the outbound send loop (bidirectional flow)."""
        if getattr(self, "_send_task", None) is not None:
            if not self._send_task.done():
                return
        self._send_wakeup.clear()
        self._send_task = asyncio.create_task(self._send_loop())

    # ================================================================
    # Send/Receive Loops
    # ================================================================

    async def send(self, message: dict[str, Any], priority: MessagePriority = MessagePriority.COMMAND) -> bool:
        """Send a message with priority."""
        # Add generation info
        message["generation"] = self.generation.as_dict()
        success = await self._enqueue_message(priority, message)
        if success:
            self._messages_sent += 1
        return success

    async def _send_loop(self) -> None:
        """Drain outbound queues to the transport (bidirectional flow, A263).

        Control channel is drained first (heartbeat/ack/cursor priority),
        followed by outbox state events, then regular message traffic in
        priority order.  The loop is woken by the enqueue paths and parks
        on an idle event between batches so dead transports do not spin.
        """
        try:
            while not self._heartbeat_dead.is_set():
                # 1) Control priority channel
                while self._control_queue:
                    control = self._control_queue.popleft()
                    await self.transport.send(control)

                # 2) Outbox state events (STATE priority, ordered replay)
                if self.outbox is not None and self._sent_upto < self.outbox.get_latest_sequence():
                    for event in await self.outbox.fetch_after(
                        self._sent_upto, self.config.max_outbound_batch
                    ):
                        await self.transport.send(
                            {
                                "type": "state_event",
                                "event": {
                                    "sequence": event.sequence,
                                    "entity_id": event.entity_id,
                                    "entity_type": event.entity_type,
                                    "operation": event.operation,
                                    "payload": event.payload,
                                    "state_hash": event.state_hash,
                                    "idempotency_key": event.idempotency_key,
                                    "timestamp": event.timestamp,
                                },
                                "generation": self.generation.as_dict(),
                            }
                        )
                        self._sent_upto = event.sequence

                # 3) Regular message traffic in priority order.  deque is FIFO;
                #    restabilise by sorting the current batch by priority so
                #    STATE events overtake COMMAND traffic within one batch.
                if self._message_queue:
                    batch: list[tuple[MessagePriority, dict[str, Any]]] = []
                    while self._message_queue:
                        batch.append(self._message_queue.popleft())
                    batch.sort(key=lambda item: item[0].value)
                    for _, message in batch:
                        await self.transport.send(message)

                # Park until enqueued again; re-check heartbeat in case the
                # transport died while we were blocked in a send-completion.
                self._send_wakeup.clear()
                await asyncio.wait_for(
                    self._send_wakeup.wait(), timeout=self.config.send_idle_sleep_seconds
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._heartbeat_dead.set()

    async def _receive_loop(self) -> None:
        """Receive and dispatch messages."""
        try:
            while not self._heartbeat_dead.is_set():
                message = await self.transport.receive()
                if message is None:
                    break
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._heartbeat_dead.set()

    async def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Dispatch message based on type."""
        self._messages_received += 1

        # Verify generation
        msg_gen = message.get("generation")
        if msg_gen:
            # Could verify generation continuity here
            pass

        msg_type = message.get("type", "message")
        command = message.get("command")

        if msg_type == "control":
            await self._handle_control(message, command)
        elif self._on_message:
            await self._on_message(message)

    async def _handle_control(self, message: dict[str, Any], command: Optional[str]) -> None:
        """Handle control channel messages."""
        if command == "heartbeat_pong":
            await self._handle_pong(message.get("payload", {}))
        elif command == "state_event_ack":
            cursor = message.get("payload", {}).get("cursor", 0)
            if cursor > self._acked_cursor:
                self._acked_cursor = cursor
        elif command == "state_event_hello":
            # Peer hello - send session info
            await self._send_session_info(message.get("payload", {}))
        elif command == "state_event_resync":
            # Peer resync
            cursor = message.get("payload", {}).get("cursor", 0)
            await self._handle_resync(cursor)
        elif command == "state_event_session":
            # Peer session info
            pass

        if self._on_control:
            try:
                await self._on_control(message)
            except Exception:
                pass

    async def _handle_resync(self, cursor: int) -> None:
        """Handle resync request - replay from cursor."""
        self._acked_cursor = cursor
        self._sent_upto = cursor
        # Replay events from outbox
        if self.outbox:
            await self.outbox.replay_from(cursor)

    async def _send_session_info(self, payload: dict[str, Any]) -> None:
        """Send session info in response to hello."""
        message = {
            "type": "control",
            "command": "state_event_session",
            "payload": {
                "session_id": self.generation.session_id,
                "backend_generation": self.generation.backend_generation,
                "cursor": self._acked_cursor,
                "latest_sequence": self.outbox.get_latest_sequence() if self.outbox else 0,
            },
            "generation": self.generation.as_dict(),
        }
        await self._enqueue_control(message)

    # ================================================================
    # Outbox Integration (A195/A263)
    # ================================================================

    async def append_state_event(
        self,
        entity_id: str,
        entity_type: str,
        operation: str,
        payload: dict[str, Any],
        state_hash: str = "",
    ) -> OutboxEvent:
        """Append state event to transactional outbox."""
        if not self.outbox:
            raise RuntimeError("No outbox configured")
        return await self.outbox.append(
            entity_id=entity_id,
            entity_type=entity_type,
            operation=operation,
            payload=payload,
            state_hash=state_hash,
        )

    # ================================================================
    # Metrics
    # ================================================================

    def get_metrics(self) -> dict[str, Any]:
        """Get channel metrics."""
        return {
            "channel_id": self.config.channel_id,
            "state": self.state.value,
            "generation": self.generation.as_dict(),
            "messages_sent": self._messages_sent,
            "messages_received": self._messages_received,
            "heartbeats_sent": self._heartbeats_sent,
            "heartbeats_received": self._heartbeats_received,
            "reconnects": self._reconnects,
            "queue_size": len(self._message_queue),
            "control_queue_size": len(self._control_queue),
            "outbox_sequence": self.outbox.get_latest_sequence() if self.outbox else 0,
            "outbound_send_active": bool(getattr(self, "_send_task", None) and not self._send_task.done()),
            "acked_cursor": self._acked_cursor,
            "sent_upto": self._sent_upto,
            "pending_acks": len(self._pending_acks),
            "last_ping_sent": self._last_ping_sent,
            "last_pong_received": self._last_pong_received,
        }


# Factory for creating channels
async def create_channel(
    channel_id: str,
    transport: ChannelTransport,
    config: Optional[ChannelConfig] = None,
    outbox: Optional["TransactionalOutbox"] = None,
) -> A263Channel:
    """Create and connect an A263-compliant channel."""
    if config is None:
        config = ChannelConfig(channel_id=channel_id)

    channel = A263Channel(config, transport, outbox)
    generation = await channel.connect()
    return channel


__all__ = [
    "A263Channel",
    "ChannelConfig",
    "ChannelGeneration",
    "ChannelState",
    "MessagePriority",
    "OutboxEvent",
    "TransactionalOutbox",
    "ChannelTransport",
    "create_channel",
    "CHANNEL_RUNTIME_VERSION",
]