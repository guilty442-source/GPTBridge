"""DSN purpose separation.

Four purposes, four credentials, never shared:

    RUNTIME    tool/transport traffic (least privilege login role)
    READER     read-only queries
    ADMIN      bootstrap / migration / role deployment / disaster recovery
    BACKUP     pg_dump / pg_restore

``GPTBRIDGE_POSTGRES_ADMIN_DSN`` must never be visible to a runtime process:
:func:`resolve_dsn` refuses admin purposes in a runtime context, and
:func:`assert_no_admin_privileges` verifies the connected role itself is not a
superuser / CREATEDB / CREATEROLE / BYPASSRLS account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Final

from psycopg.conninfo import conninfo_to_dict


class DsnPurpose(Enum):
    RUNTIME = "runtime"
    READER = "reader"
    ADMIN = "admin"
    BACKUP = "backup"


_PURPOSE_ENV: Final[dict[DsnPurpose, tuple[str, ...]]] = {
    DsnPurpose.RUNTIME: ("GPTBRIDGE_POSTGRES_DSN",),
    DsnPurpose.READER: (
        "GPTBRIDGE_POSTGRES_READER_DSN",
        "GPTBRIDGE_XINGCHENG_READER_DSN",
    ),
    DsnPurpose.ADMIN: ("GPTBRIDGE_POSTGRES_ADMIN_DSN",),
    DsnPurpose.BACKUP: (
        "GPTBRIDGE_POSTGRES_BACKUP_DSN",
        "GPTBRIDGE_POSTGRES_ADMIN_DSN",
    ),
}

RUNTIME_CONTEXT_ENV: Final[str] = "GPTBRIDGE_RUNTIME_CONTEXT"

# Purposes allowed to run DDL / role deployment / restore.
_ELEVATED_PURPOSES: Final[frozenset[DsnPurpose]] = frozenset(
    {DsnPurpose.ADMIN, DsnPurpose.BACKUP}
)


class DsnPolicyError(RuntimeError):
    """Raised when a DSN request violates the purpose separation policy."""


def runtime_context_active() -> bool:
    value = str(os.environ.get(RUNTIME_CONTEXT_ENV, "")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DsnBinding:
    purpose: DsnPurpose
    env_name: str
    dsn: str

    @property
    def database(self) -> str:
        return str(conninfo_to_dict(self.dsn).get("dbname") or "")

    @property
    def user(self) -> str:
        return str(conninfo_to_dict(self.dsn).get("user") or "")

    def password_present(self) -> bool:
        return bool(conninfo_to_dict(self.dsn).get("password"))


def resolve_dsn(
    purpose: DsnPurpose,
    *,
    environ: dict[str, str] | None = None,
    allow_elevated: bool = False,
) -> DsnBinding:
    """Resolve the DSN for one purpose, enforcing runtime isolation.

    Resolution order (G89): an env value of the form ``credman:GPTBridge/…``
    is resolved through the governed credential store; when no env var is
    set the canonical store target ``GPTBridge/postgres/dsn/<purpose>`` is
    consulted; a plain env value remains the development fallback.
    """
    from . import credential_store

    env = environ if environ is not None else os.environ
    if purpose in _ELEVATED_PURPOSES and runtime_context_active() and not allow_elevated:
        raise DsnPolicyError(
            f"ADMIN_DSN_NOT_AVAILABLE_IN_RUNTIME_CONTEXT:{purpose.value}"
        )
    for name in _PURPOSE_ENV[purpose]:
        value = str(env.get(name, "")).strip()
        if not value:
            continue
        if credential_store.is_credential_reference(value):
            try:
                resolved = credential_store.resolve_credential_reference(value)
            except Exception as exc:
                raise DsnPolicyError(
                    f"DSN_CREDENTIAL_REFERENCE_UNAVAILABLE:{name}"
                ) from exc
            if resolved is None:
                raise DsnPolicyError(
                    f"DSN_CREDENTIAL_REFERENCE_UNRESOLVED:{name}"
                )
            return DsnBinding(purpose=purpose, env_name=name, dsn=resolved)
        return DsnBinding(purpose=purpose, env_name=name, dsn=value)
    try:
        stored = credential_store.read_secret(
            credential_store.DSN_TARGET_TEMPLATE.format(purpose=purpose.value)
        )
    except Exception as exc:
        raise DsnPolicyError(
            f"DSN_CREDENTIAL_STORE_UNAVAILABLE:{purpose.value}"
        ) from exc
    if stored:
        return DsnBinding(
            purpose=purpose,
            env_name=f"credman:{credential_store.DSN_TARGET_TEMPLATE.format(purpose=purpose.value)}",
            dsn=stored,
        )
    raise DsnPolicyError(f"DSN_NOT_CONFIGURED:{purpose.value}:{','.join(_PURPOSE_ENV[purpose])}")


def assert_separated_credentials(bindings: dict[DsnPurpose, DsnBinding]) -> None:
    """Runtime/reader must not reuse the admin/backup login."""
    users = {purpose: binding.user for purpose, binding in bindings.items()}
    runtime_like = {
        purpose: users[purpose]
        for purpose in (DsnPurpose.RUNTIME, DsnPurpose.READER)
        if purpose in users
    }
    elevated = {
        purpose: users[purpose]
        for purpose in _ELEVATED_PURPOSES
        if purpose in users
    }
    overlap = set(runtime_like.values()) & set(elevated.values())
    if overlap:
        raise DsnPolicyError(
            "DSN_CREDENTIAL_REUSE_FORBIDDEN:" + ",".join(sorted(overlap))
        )


_ADMIN_ROLE_PROBE_SQL: Final[str] = (
    "SELECT current_user AS role_name, "
    "COALESCE((SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls "
    "FROM pg_roles WHERE rolname = current_user), true) AS elevated"
)


def assert_no_admin_privileges(connection) -> str:  # pragma: no cover - needs PG
    """Fail closed when the runtime connection holds elevated role flags."""
    row = connection.execute(_ADMIN_ROLE_PROBE_SQL).fetchone()
    if row is None:
        raise DsnPolicyError("ROLE_PROBE_EMPTY")
    if isinstance(row, dict):
        role = str(row.get("role_name"))
        elevated = bool(row.get("elevated"))
    else:
        role = str(row[0])
        elevated = bool(row[1])
    if elevated:
        raise DsnPolicyError(f"RUNTIME_ROLE_HAS_ADMIN_PRIVILEGES:{role}")
    return role


__all__ = [
    "DsnBinding",
    "DsnPolicyError",
    "DsnPurpose",
    "RUNTIME_CONTEXT_ENV",
    "assert_no_admin_privileges",
    "assert_separated_credentials",
    "resolve_dsn",
    "runtime_context_active",
]
