"""Short-term session identity.

Every connection starts anonymous; before any SQL runs the caller binds the
current request identity for the duration of the transaction only:

    actor_id, module_id, request_id, decision_id, correlation_id

The values are applied with ``set_config(name, value, true)`` (transaction
local) using bound parameters — never string interpolation — so RLS policies,
provenance triggers and transport rows can all attribute the statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Iterable

_SESSION_VARIABLES: Final[tuple[str, ...]] = (
    "gptbridge.actor_id",
    "gptbridge.module_id",
    "gptbridge.request_id",
    "gptbridge.decision_id",
    "gptbridge.correlation_id",
)

_SET_LOCAL_SQL: Final[str] = "SELECT set_config(%s, %s, true)"

_READ_SQL: Final[str] = (
    "SELECT "
    + ", ".join(f"current_setting('{name}', true) AS {name.split('.')[1]}" for name in _SESSION_VARIABLES)
)


@dataclass(frozen=True)
class SessionIdentity:
    actor_id: str
    module_id: str
    request_id: str = ""
    decision_id: str = ""
    correlation_id: str = ""

    def variables(self) -> dict[str, str]:
        return {
            "gptbridge.actor_id": self.actor_id.strip(),
            "gptbridge.module_id": self.module_id.strip(),
            "gptbridge.request_id": self.request_id.strip(),
            "gptbridge.decision_id": self.decision_id.strip(),
            "gptbridge.correlation_id": self.correlation_id.strip(),
        }

    def validate(self) -> None:
        values = self.variables()
        if not values["gptbridge.actor_id"]:
            raise ValueError("SESSION_IDENTITY_ACTOR_REQUIRED")
        if not values["gptbridge.module_id"]:
            raise ValueError("SESSION_IDENTITY_MODULE_REQUIRED")
        for name, value in values.items():
            if len(value) > 256:
                raise ValueError(f"SESSION_IDENTITY_VALUE_TOO_LONG:{name}")
            if "\x00" in value:
                raise ValueError(f"SESSION_IDENTITY_VALUE_INVALID:{name}")


def apply_session_identity(connection: Any, identity: SessionIdentity) -> list[tuple[str, str]]:
    """Bind the identity to the current transaction (execute the returned params)."""
    identity.validate()
    applied: list[tuple[str, str]] = []
    for name, value in identity.variables().items():
        connection.execute(_SET_LOCAL_SQL, (name, value))  # sql-ok: set_config per session variable (5 fixed vars)
        applied.append((name, value))
    return applied


def read_session_identity(connection: Any) -> dict[str, str]:  # pragma: no cover - needs PG
    row = connection.execute(_READ_SQL).fetchone()
    if row is None:
        return {}
    return {name.split(".")[1]: str(value or "") for name, value in zip(_SESSION_VARIABLES, row)}


def identity_sql_statements(identity: SessionIdentity) -> list[str]:
    """Rendered statements for diagnostics/tests (values already quoted)."""
    identity.validate()
    return [
        f"SET LOCAL {name} = '{value.replace(chr(39), chr(39) * 2)}'"
        for name, value in identity.variables().items()
    ]


__all__ = [
    "SessionIdentity",
    "apply_session_identity",
    "identity_sql_statements",
    "read_session_identity",
]
