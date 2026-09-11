"""Transactional outbox publisher — event delivery and ack cursors (A195).

Per Governance Codex A195:

  * ``DELIVERY:at-least-once+idempotency-key+per-entity-monotonic-revision+
    per-stream-monotonic-sequence+client-acknowledged-cursor``.
  * ``PUBLISH:outbox>information-layer authentication/authorization/type/
    field/redaction/order gates>all-authorized-active-frontend-sessions``.
  * ``RECONNECT:reauthenticate+resubscribe+send-last-acknowledged-cursor+
    gap-check+snapshot-or-replay+ready-only-after-convergence``.
  * ``BACKPRESSURE:bounded-buffer``.

Sessions exist only for WebSocket connections that already passed
``_websocket_request_authorized`` — the authentication/authorization gates
are therefore enforced before any event leaves the outbox.

The durable store lives in :mod:`tasks.state_outbox_store` (split for
A185/E160 source-size compliance).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from core_system.versioning import component_version
from tasks.state_outbox_store import (
    DELIVERY_WINDOW,
    DRAIN_BATCH_LIMIT,
    OUTBOX_CONTRACT_VERSION,
    OUTBOX_EVENT_NAME,
    OUTBOX_RELATIVE_PATH,
    POLL_INTERVAL_SECONDS,
    RETRY_INTERVAL_SECONDS,
    OutboxStore,
    _EVENT_REQUIRED_FIELDS,
)


class OutboxPublisher:
    """Delivers outbox events to authenticated sessions (A195 DELIVERY).

    Per-session state:

    * ``acked`` — client-acknowledged cursor (only moves on ``state_event_ack``)
    * ``sent_upto`` — highest sequence already delivered to this session
    * bounded window ``(acked, acked + DELIVERY_WINDOW]`` prevents unbounded
      buffering (A195 BACKPRESSURE); unacked batches are re-sent after
      ``RETRY_INTERVAL_SECONDS`` (at-least-once + client idempotency).
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        project_root = Path(getattr(app, "project_root", Path.cwd())).resolve()
        self._store = OutboxStore(project_root)
        self._backend_generation = uuid.uuid4().hex
        self._release_id = component_version("main-system")
        self._sessions: dict[int, dict[str, Any]] = {}
        self._wake = asyncio.Event()

    @property
    def store(self) -> OutboxStore:
        return self._store

    @property
    def backend_generation(self) -> str:
        return self._backend_generation

    # ------------------------------------------------------------------
    # producer side — called by the committing component
    # ------------------------------------------------------------------

    def append_state_event(
        self,
        *,
        entity_id: str,
        entity_type: str,
        operation: str,
        changed_field_allowlist: Iterable[str] = (),
        invalidation_keys: Iterable[str] = (),
        state_hash: str = "",
        correlation_id: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Commit-side append (A195 STATE-CHANGE) then wake the publisher."""
        event = self._store.append(
            entity_id=entity_id,
            entity_type=entity_type,
            operation=operation,
            changed_field_allowlist=changed_field_allowlist,
            invalidation_keys=invalidation_keys,
            state_hash=state_hash,
            backend_generation=self._backend_generation,
            release_id=self._release_id,
            contract_version=OUTBOX_CONTRACT_VERSION,
            correlation_id=correlation_id,
            connection=connection,
        )
        event["idempotency_key"] = f"{self._backend_generation}:{event['sequence']}"
        self._wake.set()
        return event

    # ------------------------------------------------------------------
    # session lifecycle — called by the WebSocket handler
    # ------------------------------------------------------------------

    def register_session(self, ui: Any) -> str:
        session_id = uuid.uuid4().hex
        self._sessions[id(ui)] = {
            "ui": ui,
            "session_id": session_id,
            "acked": 0,
            "sent_upto": 0,
            "last_attempt": 0.0,
        }
        return session_id

    def unregister_session(self, ui: Any) -> None:
        self._sessions.pop(id(ui), None)

    def _session_for(self, ui: Any) -> dict[str, Any] | None:
        return self._sessions.get(id(ui))

    def handle_hello(self, ui: Any, cursor: Any) -> dict[str, Any]:
        """RECONNECT: client resubscribes with its last acknowledged cursor."""
        session = self._session_for(ui)
        if session is None:
            session_id = self.register_session(ui)
            session = self._sessions[id(ui)]
        else:
            session_id = session["session_id"]
        try:
            cursor_int = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            cursor_int = 0
        session["acked"] = cursor_int
        session["sent_upto"] = cursor_int
        session["last_attempt"] = 0.0
        self._wake.set()
        return {
            "session_id": session_id,
            "backend_generation": self._backend_generation,
            "release_id": self._release_id,
            "contract_version": OUTBOX_CONTRACT_VERSION,
            "cursor": cursor_int,
            "latest_sequence": self._store.max_sequence(),
        }

    def handle_ack(self, ui: Any, cursor: Any) -> None:
        """Client-acknowledged cursor — never moves backwards (A195 ORDER)."""
        session = self._session_for(ui)
        if session is None:
            return
        try:
            ack = int(cursor)
        except (TypeError, ValueError):
            return
        if ack > session["acked"]:
            session["acked"] = ack
        self._prune()

    def handle_resync(self, ui: Any, cursor: Any) -> dict[str, Any]:
        """GAP: reset the session cursor; replay resumes after it."""
        session = self._session_for(ui)
        if session is None:
            return self.handle_hello(ui, 0)
        try:
            cursor_int = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            cursor_int = 0
        session["acked"] = cursor_int
        session["sent_upto"] = session["acked"]
        session["last_attempt"] = 0.0
        self._wake.set()
        return {
            "session_id": session["session_id"],
            "cursor": session["acked"],
            "resync": True,
        }

    # ------------------------------------------------------------------
    # delivery loop
    # ------------------------------------------------------------------

    async def run(self, shutdown_event: asyncio.Event) -> None:
        while not shutdown_event.is_set():
            try:
                self._wake.clear()
                await self._drain()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=POLL_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    @staticmethod
    def _gate_event(event: dict[str, Any]) -> bool:
        """Information-layer type/field gate before delivery (A195 PUBLISH)."""
        return all(field in event for field in _EVENT_REQUIRED_FIELDS)

    async def _drain(self) -> None:
        if not self._sessions:
            return
        now = time.monotonic()
        dead: list[int] = []
        for key, session in list(self._sessions.items()):
            ui = session["ui"]
            # Retry unacked deliveries after the retry interval; otherwise
            # send only what has not been sent, bounded by the window.
            window_end = session["acked"] + DELIVERY_WINDOW
            start_after = session["sent_upto"]
            if (
                session["sent_upto"] > session["acked"]
                and now - session["last_attempt"] >= RETRY_INTERVAL_SECONDS
            ):
                start_after = session["acked"]
            if start_after >= window_end:
                continue
            events = self._store.fetch_after(
                start_after, limit=min(DRAIN_BATCH_LIMIT, window_end - start_after)
            )
            for event in events:
                if event["sequence"] > window_end:
                    break
                if not self._gate_event(event):
                    continue
                event = dict(event)
                event["idempotency_key"] = (
                    f"{self._backend_generation}:{event['sequence']}"
                )
                event["session_id"] = session["session_id"]
                try:
                    await ui.send_event(OUTBOX_EVENT_NAME, event)
                except Exception:
                    dead.append(key)
                    break
                session["sent_upto"] = event["sequence"]
            session["last_attempt"] = now
        for key in dead:
            self._sessions.pop(key, None)

    def _prune(self) -> None:
        if self._sessions:
            floor = min(s["acked"] for s in self._sessions.values())
        else:
            floor = self._store.max_sequence()
        try:
            self._store.prune(below_sequence=floor)
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "backend_generation": self._backend_generation,
            "release_id": self._release_id,
            "contract_version": OUTBOX_CONTRACT_VERSION,
            "latest_sequence": self._store.max_sequence(),
            "sessions": [
                {
                    "session_id": s["session_id"],
                    "acked": s["acked"],
                    "sent_upto": s["sent_upto"],
                    "lag": max(0, s["sent_upto"] - s["acked"]),
                }
                for s in self._sessions.values()
            ],
        }


__all__ = [
    "OUTBOX_CONTRACT_VERSION",
    "OUTBOX_EVENT_NAME",
    "OUTBOX_RELATIVE_PATH",
    "OutboxPublisher",
    "OutboxStore",
]
