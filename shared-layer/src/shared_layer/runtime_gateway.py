"""A177 universal information-layer connection gateway.

Per Governance Codex A177 (universal-information-layer-connection-exclusivity):

  * ``REQUIRED-ROUTE:authenticated-requester>registered-information-layer-entry>
    authorization+type+contract+scope+rate+observability+audit-gates>
    information-layer-owned-transport-adapter>authorized-destination>
    typed-result-via-information-layer``.
  * ``CREDENTIALS+TOKENS+PORTS+CONNECTION-POOLS+SESSIONS+RETRY+TIMEOUT+
    HEARTBEAT+BACKPRESSURE+RECONNECT:information-layer-custody``.
  * ``CALLER:request-only+no-transport-handle``.
  * ``DESTINATION:reply-only-through-originating-information-route``.

The gateway is the sole registered entry point for cross-owner commands.
Transports (WebSocket, PostgreSQL, IPC) are adapters owned by this layer;
callers provide only a request, never a transport handle.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Final

RouteHandler = Callable[[str, dict[str, Any]], Awaitable[tuple[str, dict[str, Any]]]]
AuditSink = Callable[[dict[str, Any]], None]
AuthorizeFn = Callable[[str, str, str], bool] | None

# A224: commands not in the directory are rejected as UNKNOWN_COMMAND_CODE.
_UNKNOWN_COMMAND: Final[str] = "UNKNOWN_COMMAND_CODE"
_INVALID_ENVELOPE: Final[str] = "INVALID_INFORMATION_CHANNEL_ENVELOPE"
_RATE_EXCEEDED: Final[str] = "RATE_LIMIT_EXCEEDED"

# Rate-limiter defaults (A177 RATE gate).
_DEFAULT_RATE_CAPACITY: Final[int] = 30
_DEFAULT_RATE_REFILL_PER_SEC: Final[float] = 10.0


def _utc_now_iso() -> str:
    """A200: UTC RFC3339 timestamp for audit records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class GovernedCommandEnvelope:
    """Typed envelope routed through the information layer (A177)."""

    sender: str
    destination: str
    command: str
    payload: dict[str, Any]
    issued_at: str = field(default_factory=_utc_now_iso)


# ---------------------------------------------------------------------------
# A224 contract gate — validates commands against command_code_directory
# ---------------------------------------------------------------------------


class CommandContractResolver:
    """Read-only resolver for the canonical command_code_directory (A224).

    Caches the full command-code set on first lookup; subsequent lookups
    hit the in-memory cache.  The SQLite file is opened read-only.
    """

    def __init__(self, project_root: Path | str) -> None:
        self._db = Path(project_root) / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
        self._cache: set[str] | None = None

    def _load(self) -> set[str]:
        if self._cache is not None:
            return self._cache
        codes: set[str] = set()
        try:
            uri = f"file:{self._db.as_posix()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            for row in conn.execute("SELECT command_code FROM command_code_directory"):
                codes.add(str(row[0]))
            conn.close()
        except Exception:
            pass  # fail-open on database error; audit gate still records
        self._cache = codes
        return codes

    def is_registered(self, command: str) -> bool:
        """Return True if *command* (UPPERCASE or kebab-case) is registered."""
        if not command:
            return False
        codes = self._load()
        upper = command.upper()
        kebab = command.replace("_", "-").replace(":", "-")
        return upper in codes or kebab in {c.replace("_", "-").lower() for c in codes}

    def invalidate(self) -> None:
        """Clear the cache so the next lookup re-reads the directory."""
        self._cache = None


# ---------------------------------------------------------------------------
# A177 RATE gate — token-bucket per sender
# ---------------------------------------------------------------------------


@dataclass
class _TokenBucket:
    capacity: int
    tokens: float
    refill_per_sec: float
    last_refill: float = field(default_factory=time.monotonic)

    def try_consume(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Per-sender token-bucket rate limiter (A177 RATE gate)."""

    def __init__(
        self,
        capacity: int = _DEFAULT_RATE_CAPACITY,
        refill_per_sec: float = _DEFAULT_RATE_REFILL_PER_SEC,
    ) -> None:
        self._capacity = max(1, int(capacity))
        self._refill = max(0.1, float(refill_per_sec))
        self._buckets: dict[str, _TokenBucket] = defaultdict(
            lambda: _TokenBucket(self._capacity, self._capacity, self._refill)
        )

    def allow(self, sender: str) -> bool:
        return self._buckets[sender].try_consume()


# ---------------------------------------------------------------------------
# A177 information-layer gateway
# ---------------------------------------------------------------------------


class InformationChannelGateway:
    """Sole registered entry point for cross-owner commands (A177).

    Pipeline: envelope-validation > contract-gate > authorization-gate >
    rate-gate > audit-gate > transport-adapter > handler > typed-result.
    """

    def __init__(
        self,
        handler: RouteHandler,
        audit: AuditSink | None = None,
        *,
        project_root: Path | str | None = None,
        authorize: AuthorizeFn = None,
        rate_capacity: int = _DEFAULT_RATE_CAPACITY,
        rate_refill_per_sec: float = _DEFAULT_RATE_REFILL_PER_SEC,
        contract_resolver: CommandContractResolver | None = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("route handler is required")
        self._handler = handler
        self._audit = audit
        self._authorize = authorize
        self._rate_limiter = RateLimiter(rate_capacity, rate_refill_per_sec)
        self._contract = contract_resolver or (
            CommandContractResolver(project_root) if project_root else None
        )
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
        """Route a governed command through the A177 gate pipeline."""
        envelope = self._validate_envelope(sender, destination, command, payload)

        # A224 contract gate
        if self._contract is not None and not self._contract.is_registered(command):
            self._emit_audit(envelope, ok=False, error=_UNKNOWN_COMMAND)
            return f"{command}_result", {
                "ok": False,
                "error_code": _UNKNOWN_COMMAND,
                "message": f"Command '{command}' is not registered in command_code_directory",
            }

        # A177 authorization gate
        if self._authorize is not None and not self._authorize(sender, destination, command):
            self._emit_audit(envelope, ok=False, error="PERMISSION_DENIED")
            return f"{command}_result", {
                "ok": False,
                "error_code": "PERMISSION_DENIED",
                "message": "Authorization denied for this command",
            }

        # A177 rate gate
        if not self._rate_limiter.allow(sender):
            self._emit_audit(envelope, ok=False, error=_RATE_EXCEEDED)
            return f"{command}_result", {
                "ok": False,
                "error_code": _RATE_EXCEEDED,
                "message": "Rate limit exceeded for this sender",
            }

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, dict[str, Any]]] = loop.create_future()
        await self._queue.put((envelope, future))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="information-channel")
        return await future

    @staticmethod
    def _validate_envelope(
        sender: str,
        destination: str,
        command: str,
        payload: dict[str, Any],
    ) -> GovernedCommandEnvelope:
        sender = str(sender or "").strip()
        destination = str(destination or "").strip()
        command = str(command or "").strip()
        if not sender or not destination or not command or not isinstance(payload, dict):
            raise PermissionError(_INVALID_ENVELOPE)
        return GovernedCommandEnvelope(sender, destination, command, dict(payload))

    def _emit_audit(
        self,
        envelope: GovernedCommandEnvelope,
        *,
        ok: bool,
        error: str = "",
    ) -> None:
        if self._audit is None:
            return
        self._audit(
            {
                "transport_owner": "shared-layer",
                "channel": "system",
                "sender": envelope.sender,
                "destination": envelope.destination,
                "command": envelope.command,
                "ok": ok,
                "error_code": error,
                "timestamp": _utc_now_iso(),  # A200
            }
        )

    async def _run(self) -> None:
        while not self._queue.empty():
            envelope, future = await self._queue.get()
            try:
                self._emit_audit(envelope, ok=True)
                result = await self._handler(envelope.command, envelope.payload)
                if not future.done():
                    future.set_result(result)
            except Exception as error:
                self._emit_audit(envelope, ok=False, error=type(error).__name__)
                if not future.done():
                    future.set_exception(error)
            finally:
                self._queue.task_done()


__all__ = [
    "CommandContractResolver",
    "GovernedCommandEnvelope",
    "InformationChannelGateway",
    "RateLimiter",
]
