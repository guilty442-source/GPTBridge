"""Audit Sink and Publication Error — A121/A46 mandatory audit publication."""

from __future__ import annotations

from typing import Any, Callable


AuditSink = Callable[[dict[str, Any]], None]


class AuditPublicationError(Exception):
    """Raised when mandatory audit publication fails (A121/A46).

    The gateway treats an unrecordable audit event as a denial;
    the error propagates so the caller never sees a silent success
    or downgraded failure.
    """
    pass


def _build_audit_record(
    envelope: Any,
    *,
    ok: bool,
    error: str = "",
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build standard audit record for a command."""
    from datetime import datetime, timezone

    record: dict[str, Any] = {
        "transport_owner": "shared-layer",
        "channel": "system",
        "sender": envelope.sender,
        "destination": envelope.destination,
        "command": envelope.command,
        "ok": ok,
        "error_code": error,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # A200
    }
    if verification is not None:
        record["verification"] = dict(verification)
    return record


def publish_audit(
    audit_sink: AuditSink,
    envelope: Any,
    *,
    ok: bool,
    error: str = "",
    verification: dict[str, Any] | None = None,
) -> None:
    """Mandatory audit publication (A121/A46); raises when unrecorded."""
    if audit_sink is None:
        raise AuditPublicationError("AUDIT_SINK_REQUIRED")
    record = _build_audit_record(envelope, ok=ok, error=error, verification=verification)
    try:
        audit_sink(record)
    except Exception as error_exc:
        raise AuditPublicationError(
            f"audit sink failed: {type(error_exc).__name__}: {str(error_exc)[:160]}"
        ) from error_exc


__all__ = ["AuditSink", "AuditPublicationError", "publish_audit"]