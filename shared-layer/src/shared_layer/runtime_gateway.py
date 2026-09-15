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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Mapping

from .audit_sink import AuditSink, AuditPublicationError, publish_audit
from .contract_resolver import CommandContractResolver
from .envelope import GovernedCommandEnvelope
from .gateway_metrics import GatewayMetrics
from .rate_limiter import RateLimiter

RouteHandler = Callable[[str, dict[str, Any]], Awaitable[tuple[str, dict[str, Any]]]]
AuthorizeFn = Callable[[str, str, str], bool] | None

# A224: commands not in the directory are rejected as UNKNOWN_COMMAND_CODE.
_UNKNOWN_COMMAND: Final[str] = "UNKNOWN_COMMAND_CODE"
_INVALID_ENVELOPE: Final[str] = "INVALID_INFORMATION_CHANNEL_ENVELOPE"
_RATE_EXCEEDED: Final[str] = "RATE_LIMIT_EXCEEDED"
_CONTRACT_UNAVAILABLE: Final[str] = "CONTRACT_GATE_UNAVAILABLE"


@dataclass(frozen=True)
class ResultVerification:
    """Independent contract verification of a handler result (A69)."""

    verified: bool
    verifier: str
    reasons: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "verifier": self.verifier,
            "reasons": list(self.reasons),
        }


def verify_handler_result(result: Any) -> ResultVerification:
    """Contract-verify a handler result; never trusts a self-declared ok."""
    verifier = "shared-layer/result-verifier"
    reasons: list[str] = []
    if not isinstance(result, Mapping):
        reasons.append("RESULT_NOT_MAPPING")
    else:
        ok = result.get("ok")
        if not isinstance(ok, bool):
            reasons.append("RESULT_OK_FLAG_MISSING")
        if "verification" in result:
            reasons.append("EXECUTOR_SELF_DECLARED_VERIFICATION")
        if ok is False and not str(result.get("error_code", "")).strip():
            reasons.append("FAILURE_WITHOUT_ERROR_CODE")
        if ok is True and str(result.get("error_code", "")).strip():
            reasons.append("SUCCESS_WITH_ERROR_CODE")
    return ResultVerification(not reasons, verifier, tuple(reasons))


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
        rate_capacity: int = 30,
        rate_refill_per_sec: float = 10.0,
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
        self._metrics = GatewayMetrics()

    async def dispatch(
        self,
        *,
        sender: str,
        destination: str,
        command: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Route a governed command through the A177 gate pipeline."""
        self._metrics.record_request()
        envelope = self._validate_envelope(sender, destination, command, payload)

        # A224 contract gate — fail closed when the directory is unreadable
        if self._contract is not None:
            if self._contract.load_error():
                self._metrics.record_denied("contract")
                return self._deny(
                    envelope,
                    _CONTRACT_UNAVAILABLE,
                    f"command_code_directory unreadable: {self._contract.load_error()}",
                )
            if not self._contract.is_registered(command):
                self._metrics.record_denied("contract")
                return self._deny(
                    envelope,
                    _UNKNOWN_COMMAND,
                    f"Command '{command}' is not registered in command_code_directory",
                )

        # A177 authorization gate
        if self._authorize is not None and not self._authorize(sender, destination, command):
            self._metrics.record_denied("authorization")
            return self._deny(
                envelope, "PERMISSION_DENIED", "Authorization denied for this command"
            )

        # A177 rate gate
        if not await self._rate_limiter.allow(sender):
            self._metrics.record_denied("rate_limit")
            return self._deny(
                envelope, _RATE_EXCEEDED, "Rate limit exceeded for this sender"
            )

        self._metrics.record_allowed()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, dict[str, Any]]] = loop.create_future()
        await self._queue.put((envelope, future))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="information-channel")
        result = await future
        return result

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

    def _deny(
        self,
        envelope: GovernedCommandEnvelope,
        code: str,
        message: str,
    ) -> tuple[str, dict[str, Any]]:
        """Audited denial; an unrecordable denial becomes an audit failure."""
        try:
            publish_audit(self._audit, envelope, ok=False, error=code)
        except AuditPublicationError as error:
            code, message = "AUDIT_PUBLICATION_FAILED", str(error)
        return (
            f"{envelope.command}_result",
            {"ok": False, "error_code": code, "message": message},
        )

    async def _run(self) -> None:
        while not self._queue.empty():
            envelope, future = await self._queue.get()
            try:
                result = await self._handler(envelope.command, envelope.payload)
                self._deliver(envelope, future, result)
            except Exception as error:
                # Handler failure: recorded with full detail and re-raised —
                # never downgraded to a generic success or refusal.
                self._fail(envelope, future, error)
            finally:
                self._queue.task_done()

    def _deliver(
        self,
        envelope: GovernedCommandEnvelope,
        future: asyncio.Future[tuple[str, dict[str, Any]]],
        result: Any,
    ) -> None:
        # Handlers return a ``(event_name, result_dict)`` pair — the A69
        # contract verification applies to the result mapping only.
        payload_result = (
            result[1]
            if isinstance(result, tuple) and len(result) == 2
            else result
        )
        verdict = verify_handler_result(payload_result)
        if not verdict.verified:
            self._metrics.record_verification_failure()
        try:
            publish_audit(
                self._audit,
                envelope,
                ok=verdict.verified,
                error="" if verdict.verified else "RESULT_VERIFICATION_FAILED",
                verification=verdict.to_record(),
            )
        except AuditPublicationError as error:
            self._resolve(
                future,
                self._deny_payload(envelope, "AUDIT_PUBLICATION_FAILED", str(error)),
            )
            return
        if not verdict.verified:
            self._resolve(
                future,
                self._deny_payload(
                    envelope,
                    "RESULT_VERIFICATION_FAILED",
                    ";".join(verdict.reasons),
                ),
            )
            return
        output = dict(payload_result)
        output["verification"] = verdict.to_record()
        self._resolve(future, (f"{envelope.command}_result", output))

    def _fail(
        self,
        envelope: GovernedCommandEnvelope,
        future: asyncio.Future[tuple[str, dict[str, Any]]],
        error: Exception,
    ) -> None:
        self._metrics.record_handler_error()
        detail = f"{type(error).__name__}: {str(error)[:200]}"
        try:
            publish_audit(
                self._audit,
                envelope,
                ok=False,
                error=type(error).__name__,
                verification={"detail": detail},
            )
        except AuditPublicationError:
            # The audit sink itself is failing; the caller is still told the
            # original failure (never a silent success or downgrade).
            detail += " (audit sink unavailable)"
        if not future.done():
            future.set_exception(error)

    @staticmethod
    def _deny_payload(
        envelope: GovernedCommandEnvelope,
        code: str,
        message: str,
    ) -> tuple[str, dict[str, Any]]:
        return (
            f"{envelope.command}_result",
            {"ok": False, "error_code": code, "message": message},
        )

    @staticmethod
    def _resolve(
        future: asyncio.Future[tuple[str, dict[str, Any]]],
        value: tuple[str, dict[str, Any]],
    ) -> None:
        if not future.done():
            future.set_result(value)

    def get_metrics(self) -> dict[str, Any]:
        """Get gateway metrics for monitoring (A177 observability)."""
        return self._metrics.get_metrics()

    def get_metrics_snapshot(self) -> dict[str, Any]:
        """Get gateway metrics with derived values."""
        return self._metrics.get_metrics_snapshot()


__all__ = [
    "CommandContractResolver",
    "GovernedCommandEnvelope",
    "InformationChannelGateway",
    "RateLimiter",
]