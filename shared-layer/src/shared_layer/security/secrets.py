"""Secret metadata registry.

Secrets never enter PostgreSQL as rows.  The database may only hold:

    secret_id, owner, version, rotated_at, expires_at, hash/fingerprint

:func:`assert_metadata_only` fail-closes on payloads that look like they
carry plaintext material, and :func:`fingerprint` provides the one-way
identity used for verification without disclosure.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Final

FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "dsn",
        "connection_string",
        "conninfo",
        "token",
        "api_key",
        "apikey",
        "secret",
        "private_key",
        "key_material",
        "client_secret",
        "refresh_token",
    }
)

MAX_FINGERPRINT_LENGTH: Final[int] = 128


class SecretPolicyError(RuntimeError):
    """Raised when a secret payload violates the metadata-only policy."""


@dataclass
class SecretMetadata:
    secret_id: str
    owner: str
    version: int
    rotated_at: float
    expires_at: float | None = None
    grace_until: float | None = None
    fingerprint: str = ""
    status: str = "active"

    def as_row(self) -> dict[str, Any]:
        return {
            "secret_id": self.secret_id,
            "owner": self.owner,
            "version": self.version,
            "rotated_at": self.rotated_at,
            "expires_at": self.expires_at,
            "grace_until": self.grace_until,
            "fingerprint": self.fingerprint,
            "status": self.status,
        }

    def expired(self, *, now: float) -> bool:
        if self.expires_at is None:
            return False
        return now >= self.expires_at


def fingerprint(secret: str, *, salt: str = "gptbridge-credential") -> str:
    """One-way fingerprint of a credential (HMAC-SHA256, hex)."""
    if not secret:
        raise SecretPolicyError("SECRET_EMPTY")
    digest = hmac.new(salt.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:64]


def verify_fingerprint(secret: str, expected: str, *, salt: str = "gptbridge-credential") -> bool:
    return hmac.compare_digest(fingerprint(secret, salt=salt), str(expected or ""))


def assert_metadata_only(payload: dict[str, Any]) -> None:
    """Reject any payload that would persist plaintext secret material."""
    lowered = {str(key).strip().casefold() for key in payload}
    offending = sorted(lowered & FORBIDDEN_PAYLOAD_KEYS)
    if offending:
        raise SecretPolicyError("SECRET_PLAINTEXT_PAYLOAD_FORBIDDEN:" + ",".join(offending))
    for key, value in payload.items():
        if not isinstance(value, str):
            continue
        if len(value) > MAX_FINGERPRINT_LENGTH:
            raise SecretPolicyError(f"SECRET_VALUE_TOO_LONG:{key}")
    unknown = lowered - {
        "secret_id",
        "owner",
        "version",
        "rotated_at",
        "expires_at",
        "grace_until",
        "fingerprint",
        "status",
    }
    if unknown:
        raise SecretPolicyError("SECRET_UNKNOWN_METADATA_FIELD:" + ",".join(sorted(unknown)))


@dataclass
class SecretRegistry:
    """Metadata-only registry (backed by ``gptbridge_security.credential``)."""

    metadata: dict[str, SecretMetadata] = field(default_factory=dict)

    def register(self, metadata: SecretMetadata) -> None:
        self.assert_registerable(metadata)
        self.metadata[metadata.secret_id] = metadata

    @staticmethod
    def assert_registerable(metadata: SecretMetadata) -> None:
        if not metadata.secret_id or not metadata.owner:
            raise SecretPolicyError("SECRET_ID_AND_OWNER_REQUIRED")
        if metadata.version < 1:
            raise SecretPolicyError("SECRET_VERSION_INVALID")
        if not metadata.fingerprint:
            raise SecretPolicyError("SECRET_FINGERPRINT_REQUIRED")
        assert_metadata_only(metadata.as_row())

    def active(self, *, now: float) -> list[SecretMetadata]:
        return [
            item
            for item in self.metadata.values()
            if item.status == "active"
            and (item.grace_until is None or now <= item.grace_until)
            and not item.expired(now=now)
        ]


__all__ = [
    "FORBIDDEN_PAYLOAD_KEYS",
    "MAX_FINGERPRINT_LENGTH",
    "SecretMetadata",
    "SecretPolicyError",
    "SecretRegistry",
    "assert_metadata_only",
    "fingerprint",
    "verify_fingerprint",
]
