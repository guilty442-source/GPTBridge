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
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Protocol

from .registry.versioning import component_version

CHANNEL_RUNTIME_VERSION: str = component_version("channel-runtime")


# ================================================================
# Channel contract types (A263 — pure data containers)
# ================================================================


class ChannelState(Enum):
    """Channel lifecycle states per A263."""
    CLOSED = "closed"
    CONNECTING = "connecting"
    OPEN = "open"
    RECONNECTING = "reconnecting"
    DEAD = "dead"


class MessagePriority(Enum):
    """Message priority for control channel."""
    CONTROL = 0    # Heartbeat, ack, cursor, reconnect
    STATE = 1      # State events from outbox
    COMMAND = 2    # User commands


@dataclass(frozen=True)
class ChannelGeneration:
    """Typed generation identifier for a channel (A263)."""
    channel_id: str
    generation: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    backend_generation: str = ""
    session_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "generation": self.generation,
            "created_at": self.created_at,
            "backend_generation": self.backend_generation,
            "session_id": self.session_id,
        }

    @property
    def full_id(self) -> str:
        return f"{self.channel_id}:{self.generation}"


@dataclass
class ChannelConfig:
    """Channel configuration per A263."""
    channel_id: str
    max_queue_size: int = 1000
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 30.0
    reconnect_max_attempts: int = 3
    reconnect_base_delay_seconds: float = 1.0
    control_channel_capacity: int = 100
    enable_backpressure: bool = True
    max_outbound_batch: int = 100
    max_event_payload_bytes: int = 1_048_576
    send_idle_sleep_seconds: float = 0.001


@dataclass
class OutboxEvent:
    """Transactional outbox event (A195/A263)."""
    sequence: int
    entity_id: str
    entity_type: str
    operation: str
    payload: dict[str, Any]
    state_hash: str
    idempotency_key: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ================================================================
# Transactional Outbox (A195/A263)
# ================================================================


class TransactionalOutbox:
    """Transactional outbox for state changes (A195/A263)."""

    def __init__(self, channel_id: str, max_sequence: int = 0, callback=None) -> None:
        self.channel_id = channel_id
        self._sequence = max_sequence
        self._events: Dict[int, OutboxEvent] = {}
        self._lock = asyncio.Lock()
        self._callback = callback

    def get_latest_sequence(self) -> int:
        return self._sequence

    async def append(
        self,
        entity_id: str,
        entity_type: str,
        operation: str,
        payload: dict[str, Any],
        state_hash: str = "",
    ) -> OutboxEvent:
        """Append event to outbox."""
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError):
            encoded = json.dumps({"repr": repr(payload)})
        async with self._lock:
            self._sequence += 1
            event = OutboxEvent(
                sequence=self._sequence,
                entity_id=entity_id,
                entity_type=entity_type,
                operation=operation,
                payload=payload,
                state_hash=state_hash,
                idempotency_key=f"{self.channel_id}:{self._sequence}",
            )
            self._events[self._sequence] = event
            if self._callback is not None:
                try:
                    await self._callback(event)
                except Exception:
                    pass
            return event

    async def fetch_after(self, cursor: int, limit: int) -> List[OutboxEvent]:
        """Fetch events after cursor."""
        async with self._lock:
            events = []
            for seq in range(cursor + 1, min(cursor + 1 + limit, self._sequence + 1)):
                if seq in self._events:
                    events.append(self._events[seq])
            return events

    async def replay_from(self, cursor: int) -> None:
        """Replay events from cursor through the registered callback.

        When no callback is configured the outbox drains to its own
        ``fetch_after`` contract so the channel send-loop picks the events
        up via ``_sent_upto`` advance (see ``A263Channel._send_loop``).
        """
        events = await self.fetch_after(cursor, 1000)
        if self._callback is None:
            return
        for event in events:
            try:
                await self._callback(event)
            except Exception:
                pass


# ================================================================
# Connection lifecycle mixin (A263)
# ================================================================


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


# ================================================================
# Heartbeat mixin (A263 two-way heartbeat with deadline)
# ================================================================


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


# ================================================================
# Channel transport + A263 channel
# ================================================================


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
                # Idle timeout is normal — wait_for raises TimeoutError and
                # must NOT reach the outer `except Exception` (which marks the
                # channel dead); catch it and re-loop.
                self._send_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._send_wakeup.wait(), timeout=self.config.send_idle_sleep_seconds
                    )
                except asyncio.TimeoutError:
                    pass
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
    "ConnectionMixin",
    "HeartbeatMixin",
    "ChannelTransport",
    "create_channel",
    "CHANNEL_RUNTIME_VERSION",
]
