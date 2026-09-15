"""Channel data classes — A263 channel contract types.

Pure data structures for A263-compliant channel abstraction.
No business logic — only typed data containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


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