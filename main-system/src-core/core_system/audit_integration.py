"""Central-audit integration adapter (A448/A451).

Bridges runtime audit events — the information-channel gateway audit and the
synchronization journal — to the central audit ledger
``gptbridge_audit.event``.  Before this adapter those paths only wrote a core
log entry or a JSON journal file; the central ledger is now the primary sink.

Contract
--------
* Only the existing contract columns are written:
  ``event_id / actor_id / module_id / resource_id / action / outcome /
  decision_id / details`` through the governed, parameterized ``audit.insert``
  template — no new tables, no new columns, no free-form SQL.
* Events are bounded and content-free (A435/A448): identities are trimmed,
  ``details`` is key/value capped, unknown objects degrade to a type marker,
  and secret-policy violations raise instead of being stored or downgraded.
* ``correlation_id`` / ``generation`` are carried inside ``details`` exactly
  as supplied by the caller; an empty value means unknown, never fabricated.

Availability semantics (explicit per call site)
-----------------------------------------------
* No connection provider configured -> ``AUDIT_UNAVAILABLE``; the adapter
  never touches the network and never raises.  The call site keeps its
  existing behaviour (server_commands falls back to the core log; the sync
  journal keeps the durable JSON record plus the outbox publisher).
* Provider configured but the append or commit fails -> with
  ``fail_open=True`` the adapter returns ``AUDIT_FAILED`` so the caller can
  fall back; with ``fail_open=False`` it raises ``AuditIntegrationError`` so
  the caller refuses the operation (the sync journal's A121/A446 semantics).
* A secret-policy violation always raises ``SecretPolicyError`` regardless of
  ``fail_open`` — a record that would leak credentials must never be written
  anywhere (A435).
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Final, Iterator, Mapping

from shared_layer.security.audit import (
    AuditEventError,
    CentralAuditEvent,
    emit_central,
)
from shared_layer.security.secrets import SecretPolicyError

AUDIT_RECORDED: Final[str] = "recorded"
AUDIT_UNAVAILABLE: Final[str] = "unavailable"
AUDIT_FAILED: Final[str] = "failed"
RUNTIME_DSN_ENV: Final[str] = "GPTBRIDGE_POSTGRES_DSN"


class AuditIntegrationError(RuntimeError):
    """Raised when a central-audit append is required but cannot be made."""


@contextmanager
def _connection_scope(factory: Callable[[], Any]) -> Iterator[Any]:
    """Yield a connection from a plain connection or a context manager."""
    resource = factory()
    if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
        with resource as connection:
            yield connection
        return
    yield resource


class CentralAuditAdapter:
    """Bounded central-audit writer with explicit fail-open/fail-closed policy."""

    def __init__(
        self,
        connection_factory: Callable[[], Any] | None = None,
        *,
        module_id: str = "main-system",
        fail_open: bool = True,
        failure_cooldown_seconds: float = 30.0,
    ) -> None:
        self._connection_factory = connection_factory if callable(connection_factory) else None
        self._module_id = str(module_id or "main-system")
        self._fail_open = bool(fail_open)
        self._failure_cooldown = max(0.0, float(failure_cooldown_seconds))
        self._cooldown_until = 0.0

    @property
    def available(self) -> bool:
        """True when a connection provider is configured (not a liveness probe)."""
        return self._connection_factory is not None

    def _cooldown_active(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _note_failure(self) -> None:
        if self._failure_cooldown > 0:
            self._cooldown_until = time.monotonic() + self._failure_cooldown

    def record_event(
        self,
        *,
        action: str,
        outcome: str,
        actor_id: str = "",
        resource_id: str = "",
        decision_id: str = "",
        correlation_id: str = "",
        generation: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> str:
        """Append one bounded event; returns recorded/unavailable/failed."""
        if self._connection_factory is None:
            return AUDIT_UNAVAILABLE
        if self._cooldown_active():
            return AUDIT_FAILED
        event = CentralAuditEvent(
            action=str(action or ""),
            outcome=str(outcome or ""),
            actor_id=str(actor_id or ""),
            module_id=self._module_id,
            resource_id=str(resource_id or ""),
            decision_id=str(decision_id or ""),
            correlation_id=str(correlation_id or ""),
            generation=str(generation or ""),
            details=dict(details or {}),
        )
        try:
            with _connection_scope(self._connection_factory) as connection:
                emit_central(connection, event)
                commit = getattr(connection, "commit", None)
                if callable(commit):
                    commit()
        except SecretPolicyError:
            # Content-free boundary: reject, never store and never fall back.
            raise
        except AuditEventError as error:
            raise AuditIntegrationError(f"AUDIT_EVENT_INVALID:{error}") from error
        except Exception as error:
            self._note_failure()
            if not self._fail_open:
                raise AuditIntegrationError(
                    f"AUDIT_APPEND_FAILED:{type(error).__name__}"
                ) from error
            return AUDIT_FAILED
        return AUDIT_RECORDED

    def record_gateway_record(self, record: Mapping[str, Any]) -> str:
        """Map an information-channel gateway audit record (metadata only).

        The gateway record (``shared_layer.audit_sink``) carries no content or
        secret material; only its bounded metadata is forwarded.  The record's
        ``verification`` block is passed through the same detail bounds.
        """
        if not isinstance(record, Mapping):
            raise AuditIntegrationError("AUDIT_RECORD_NOT_A_MAPPING")
        details: dict[str, Any] = {
            "channel": record.get("channel", ""),
            "transport_owner": record.get("transport_owner", ""),
            "destination": record.get("destination", ""),
            "ok": bool(record.get("ok")),
            "error_code": record.get("error_code", ""),
            "timestamp": record.get("timestamp", ""),
        }
        verification = record.get("verification")
        if isinstance(verification, Mapping):
            details["verification"] = dict(verification)
        return self.record_event(
            action=str(record.get("command") or ""),
            outcome="success" if record.get("ok") else "failure",
            actor_id=str(record.get("sender") or ""),
            resource_id=str(record.get("destination") or ""),
            correlation_id=str(record.get("correlation_id") or ""),
            generation=str(record.get("generation") or ""),
            details=details,
        )


class _RuntimePoolProvider:
    """Lazy least-privilege runtime pool used only when a DSN is configured.

    The pool is opened on first append and reused; it resolves the runtime
    DSN through ``DatabaseSettings.from_environment`` so the admin credential
    is never reused by the audit path (A501/A503).
    """

    def __init__(self) -> None:
        self._manager: Any = None
        self._lock = threading.Lock()

    def __call__(self) -> Any:
        if self._manager is None:
            with self._lock:
                if self._manager is None:
                    from shared_layer.database.config import DatabaseSettings
                    from shared_layer.database.connection import ConnectionManager

                    settings = DatabaseSettings.from_environment()
                    manager = ConnectionManager(settings, min_size=1, max_size=2)
                    manager.open()
                    self._manager = manager
        return self._manager.connection()

    def close(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.close()
                self._manager = None


def connection_provider_from_app(app: Any) -> Callable[[], Any] | None:
    """Resolve a connection provider for ``app`` without creating a second copy.

    Precedence: an explicitly injected factory on the app
    (``audit_connection_factory`` / ``_audit_connection_factory``), then the
    configured runtime DSN.  No provider means the caller must use its
    documented no-central-audit semantics.
    """
    for name in ("audit_connection_factory", "_audit_connection_factory"):
        candidate = getattr(app, name, None)
        if callable(candidate):
            return candidate
    if os.environ.get(RUNTIME_DSN_ENV, "").strip():
        return _RuntimePoolProvider()
    return None


def build_audit_adapter(
    app: Any,
    *,
    fail_open: bool = True,
    module_id: str = "main-system",
) -> CentralAuditAdapter:
    """Build the adapter for one runtime surface with an explicit failure mode."""
    return CentralAuditAdapter(
        connection_provider_from_app(app),
        module_id=module_id,
        fail_open=fail_open,
    )


__all__ = [
    "AUDIT_FAILED",
    "AUDIT_RECORDED",
    "AUDIT_UNAVAILABLE",
    "RUNTIME_DSN_ENV",
    "AuditIntegrationError",
    "CentralAuditAdapter",
    "build_audit_adapter",
    "connection_provider_from_app",
]
