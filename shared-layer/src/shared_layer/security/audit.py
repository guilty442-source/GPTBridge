"""Credential audit events.

Every credential lifecycle event is written to the central audit
(``gptbridge_audit.event``) as metadata only — never the secret itself:

    created / used / rotated / revoked / login_failed / permission_denied

:func:`build_event` produces the exact column payload for the governed
``audit.insert`` query, and :func:`assert_no_secret_material` blocks any
payload that looks like it leaks credentials.

The same module owns the bounded, content-free central-audit contract used
by runtime components (:class:`CentralAuditEvent` + :func:`emit_central`,
A448/A451): an event is trimmed to bounded metadata, secret material is
rejected (never truncated), and ``correlation_id`` / ``generation`` are
carried in ``details`` exactly as supplied — empty means unknown, never
fabricated.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Mapping

from .secrets import FORBIDDEN_PAYLOAD_KEYS, SecretPolicyError


class CredentialEvent(Enum):
    CREATED = "credential_created"
    USED = "credential_used"
    ROTATED = "credential_rotated"
    REVOKED = "credential_revoked"
    LOGIN_FAILED = "credential_login_failed"
    PERMISSION_DENIED = "credential_permission_denied"


AUDIT_TABLE: Final[str] = "gptbridge_audit.event"
AUDIT_INSERT_QUERY_KEY: Final[str] = "audit.insert"


@dataclass(frozen=True)
class CredentialAuditEvent:
    event: CredentialEvent
    actor_id: str
    module_id: str
    secret_id: str = ""
    role: str = ""
    outcome: str = ""
    decision_id: str = ""
    correlation_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "event_id": f"{self.event.value}:{self.actor_id}:{self.secret_id}:{self.role}",
            "actor_id": self.actor_id,
            "module_id": self.module_id,
            "resource_id": self.secret_id or self.role,
            "action": self.event.value,
            "outcome": self.outcome or "recorded",
            "decision_id": self.decision_id or None,
            "details": {
                "secret_id": self.secret_id,
                "role": self.role,
                "correlation_id": self.correlation_id,
                **self.details,
            },
        }

    def insert_parameters(self) -> tuple[Any, ...]:
        payload = self.payload()
        return (
            payload["event_id"],
            payload["actor_id"],
            payload["module_id"],
            payload["resource_id"],
            payload["action"],
            payload["outcome"],
            payload["decision_id"],
            payload["details"],
        )


def assert_no_secret_material(payload: dict[str, Any]) -> None:
    flattened = {str(key).strip().casefold() for key in payload}
    offending = sorted(flattened & FORBIDDEN_PAYLOAD_KEYS)
    if offending:
        raise SecretPolicyError("AUDIT_PAYLOAD_CONTAINS_SECRET_FIELD:" + ",".join(offending))
    details = payload.get("details")
    if isinstance(details, dict):
        assert_no_secret_material(details)


def emit(connection, event: CredentialAuditEvent) -> None:  # pragma: no cover - needs PG
    """Write the event through the governed audit insert."""
    payload = event.payload()
    assert_no_secret_material(payload)
    connection.execute(
        "INSERT INTO gptbridge_audit.event "
        "(event_id, actor_id, module_id, resource_id, action, outcome, decision_id, details) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        event.insert_parameters(),
    )


# ---------------------------------------------------------------------------
# Bounded central-audit contract (A448/A451)
# ---------------------------------------------------------------------------

#: Maximum length of a bounded scalar audit field (identities, action, ...).
MAX_AUDIT_FIELD_LENGTH: Final[int] = 256
#: Maximum number of keys accepted in ``details``.
MAX_AUDIT_DETAIL_KEYS: Final[int] = 24
#: Maximum length of a ``details`` key.
MAX_AUDIT_DETAIL_KEY_LENGTH: Final[int] = 64
#: Maximum length of a string ``details`` value.
MAX_AUDIT_DETAIL_VALUE_LENGTH: Final[int] = 256
#: Maximum number of items accepted from a list-like ``details`` value.
MAX_AUDIT_DETAIL_ITEMS: Final[int] = 16
#: Maximum nesting depth accepted inside ``details``.
MAX_AUDIT_DETAIL_DEPTH: Final[int] = 2
#: Machine-schema code carried by every bounded event (A448).
AUDIT_SCHEMA_CODE: Final[str] = "AUDIT_EVENT_V1"
#: Redaction class carried by every bounded event (A448/A498).
AUDIT_REDACTION_CLASS: Final[str] = "metadata"

_REQUIRED_EVENT_FIELDS: Final[tuple[str, ...]] = (
    "actor_id",
    "module_id",
    "action",
    "outcome",
)


class AuditEventError(RuntimeError):
    """Raised when a bounded central-audit event cannot be constructed."""


def _bounded_text(value: Any, limit: int = MAX_AUDIT_FIELD_LENGTH) -> str:
    """Trim a value to printable, whitespace-trimmed, bounded text."""
    text = "" if value is None else str(value)
    text = "".join(character for character in text if character.isprintable())
    return text.strip()[:limit]


def _bounded_scalar(value: Any, depth: int) -> Any:
    """Bound one ``details`` value; content-bearing objects are dropped."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _bounded_text(value, MAX_AUDIT_DETAIL_VALUE_LENGTH)
    if isinstance(value, Mapping) and depth < MAX_AUDIT_DETAIL_DEPTH:
        return _bounded_details(value, depth + 1)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:MAX_AUDIT_DETAIL_ITEMS]
        return [_bounded_scalar(item, depth + 1) for item in items]
    # Unknown objects may carry content in their repr; only the type name is
    # written so the audit stays content-free (A435/A448).
    return f"<dropped:{type(value).__name__}>"


def _bounded_details(details: Mapping[str, Any], depth: int = 1) -> dict[str, Any]:
    if not isinstance(details, Mapping):
        raise AuditEventError("AUDIT_DETAILS_NOT_A_MAPPING")
    assert_no_secret_material(dict(details))
    bounded: dict[str, Any] = {}
    for key, value in list(details.items())[:MAX_AUDIT_DETAIL_KEYS]:
        name = _bounded_text(key, MAX_AUDIT_DETAIL_KEY_LENGTH)
        if not name:
            continue
        bounded[name] = _bounded_scalar(value, depth)
    return bounded


@dataclass(frozen=True)
class CentralAuditEvent:
    """Bounded, content-free event for the central audit ledger (A448/A451).

    Only the existing contract columns are written:
    ``event_id / actor_id / module_id / resource_id / action / outcome /
    decision_id / details`` (no new tables or columns).  ``correlation_id``
    and ``generation`` travel inside ``details``: callers pass what they
    actually hold, and an empty value means unknown — never fabricated.
    Content and secret material are rejected, not stored (A435/A448).
    """

    action: str
    outcome: str
    actor_id: str = ""
    module_id: str = ""
    resource_id: str = ""
    decision_id: str = ""
    correlation_id: str = ""
    generation: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""

    def event_identity(self) -> str:
        """Return the event UUID; a fresh one is minted when none was given."""
        candidate = _bounded_text(self.event_id, 64)
        if not candidate:
            return str(uuid.uuid4())
        try:
            return str(uuid.UUID(candidate))
        except ValueError as error:
            raise AuditEventError("AUDIT_EVENT_ID_NOT_A_UUID") from error

    def payload(self) -> dict[str, Any]:
        fields = {
            name: _bounded_text(getattr(self, name))
            for name in _REQUIRED_EVENT_FIELDS
        }
        for name, value in fields.items():
            if not value:
                raise AuditEventError(f"AUDIT_EVENT_FIELD_REQUIRED:{name}")
        details = {
            "schema": AUDIT_SCHEMA_CODE,
            "redaction_class": AUDIT_REDACTION_CLASS,
            "correlation_id": _bounded_text(self.correlation_id),
            "generation": _bounded_text(self.generation),
            **_bounded_details(self.details),
        }
        payload = {
            "event_id": self.event_identity(),
            **fields,
            "resource_id": _bounded_text(self.resource_id) or None,
            "decision_id": _bounded_text(self.decision_id) or None,
            "details": details,
        }
        assert_no_secret_material(payload)
        return payload

    def insert_parameters(self) -> tuple[Any, ...]:
        payload = self.payload()
        return (
            payload["event_id"],
            payload["actor_id"],
            payload["module_id"],
            payload["resource_id"],
            payload["action"],
            payload["outcome"],
            payload["decision_id"],
            json.dumps(
                payload["details"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )


def emit_central(connection, event: CentralAuditEvent) -> None:
    """Write one bounded event through the governed ``audit.insert`` template.

    The governed template is resolved from the shared query allowlist and
    receives bound parameters only — no value is ever interpolated into SQL.
    Transaction control stays with the caller: commit after this call for the
    append to be durable (A451).  This function does not raise for database
    availability; availability policy belongs to the calling adapter.
    """
    from ..database.query_allowlist import get_query

    connection.execute(get_query(AUDIT_INSERT_QUERY_KEY), event.insert_parameters())


__all__ = [
    "AUDIT_INSERT_QUERY_KEY",
    "AUDIT_REDACTION_CLASS",
    "AUDIT_SCHEMA_CODE",
    "AUDIT_TABLE",
    "AuditEventError",
    "CentralAuditEvent",
    "CredentialAuditEvent",
    "CredentialEvent",
    "MAX_AUDIT_DETAIL_DEPTH",
    "MAX_AUDIT_DETAIL_ITEMS",
    "MAX_AUDIT_DETAIL_KEYS",
    "MAX_AUDIT_DETAIL_KEY_LENGTH",
    "MAX_AUDIT_DETAIL_VALUE_LENGTH",
    "MAX_AUDIT_FIELD_LENGTH",
    "assert_no_secret_material",
    "emit",
    "emit_central",
]
