"""Transactional Outbox — A195/A263.

Implements the transactional outbox pattern for reliable state event
publishing over A263 channels. Provides ordered event sequences with
cursor-based replay capability.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from .channel_types import OutboxEvent


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
        import json
        from typing import Any as _Any
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


__all__ = ["TransactionalOutbox"]