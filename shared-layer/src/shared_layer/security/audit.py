"""Credential audit events.

Every credential lifecycle event is written to the central audit
(``gptbridge_audit.event``) as metadata only — never the secret itself:

    created / used / rotated / revoked / login_failed / permission_denied

:func:`build_event` produces the exact column payload for the governed
``audit.insert`` query, and :func:`assert_no_secret_material` blocks any
payload that looks like it leaks credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

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


__all__ = [
    "AUDIT_INSERT_QUERY_KEY",
    "AUDIT_TABLE",
    "CredentialAuditEvent",
    "CredentialEvent",
    "assert_no_secret_material",
    "emit",
]
