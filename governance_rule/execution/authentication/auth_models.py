"""Dataclasses for the governance authentication package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LauncherIdentityAttestation:
    issuer: str
    actor: str
    bound_tool_id: str
    caller_path: str
    process_id: int
    issued_at: int
    expires_at: int
    nonce: str
    key_id: str
    signature: str


@dataclass(frozen=True)
class CapabilityTokenClaims:
    version: int
    issuer: str
    audience: str
    identity_group: str
    actor: str
    bound_tool_id: str
    target_tool_id: str | None
    capability: str
    action: str
    target: str
    data_scope: str
    caller_path: str
    manifest_digest: str
    target_manifest_digest: str | None
    target_version: str | None
    resource_path: str | None
    issued_at: int
    expires_at: int
    nonce: str
    key_id: str


@dataclass(frozen=True, repr=False)
class _SigningKey:
    key_id: str
    secret: bytes
    issued_at: int
    expires_at: int


@dataclass(frozen=True, repr=False)
class _KeyRing:
    current: _SigningKey
    previous: _SigningKey | None
