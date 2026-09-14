"""Token issuance and authentication mixin (A185 split).

Contains the issue_token and authenticate_token methods extracted
from GovernanceAuthenticationService.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from governance_rule.governance_policy import governance_policy_snapshot
from governance_rule.permission_directory.execution.identity_registry import (
    PermissionRequest,
    authorize_permission_request,
    verify_manifest_digests,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

from .auth_models import (
    CapabilityTokenClaims,
    _KeyRing,
    _SigningKey,
)
from .auth_signing import (
    _b64decode,
    _b64encode,
    _canonical,
)


class TokenOperationsMixin:
    """Token issuance and authentication operations."""

    _project_root: object
    _authority: object
    _policy: object
    _lock: object
    _closed: bool
    _actor: str
    _bound_tool_id: str
    _caller_path: str
    _key_ring: _KeyRing
    _nonces: object

    def _assert_process_binding(self) -> None:
        raise NotImplementedError

    def verify_runtime_integrity(self) -> None:
        raise NotImplementedError

    def _rotate_if_due(self, now: int) -> None:
        raise NotImplementedError

    def _actor_identity_group(self) -> str:
        raise NotImplementedError

    def _resolve_key(self, key_id: str, now: int) -> _SigningKey:
        raise NotImplementedError

    def issue_token(
        self,
        *,
        target_tool_id: str | None = None,
        capability: str,
        action: str,
        target: str,
        data_scope: str,
        target_version: str | None = None,
        resource_path: str | None = None,
        ttl_seconds: int = 60,
    ) -> str:
        now = int(time.time())
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or ttl_seconds <= 0
            or ttl_seconds > self._policy.maximum_token_ttl_seconds
        ):
            raise permission_denied()
        with self._lock:
            self._assert_process_binding()
            self.verify_runtime_integrity()
            request = PermissionRequest(
                actor=self._actor,
                bound_tool_id=self._bound_tool_id,
                target_tool_id=target_tool_id,
                capability=capability,
                action=action,
                target=target,
                data_scope=data_scope,
                caller_path=self._caller_path,
                target_version=target_version,
                resource_path=resource_path,
            )
            decision = authorize_permission_request(request, self._project_root)
            self._rotate_if_due(now)
            governance = governance_policy_snapshot()
            claims = CapabilityTokenClaims(
                version=self._authority.authority_version_policy.current_version,
                issuer=governance.identity_authentication.issuer,
                audience=governance.identity_authentication.audience,
                identity_group=self._actor_identity_group(),
                actor=request.actor,
                bound_tool_id=request.bound_tool_id,
                target_tool_id=request.target_tool_id,
                capability=request.capability,
                action=request.action,
                target=request.target,
                data_scope=request.data_scope,
                caller_path=request.caller_path,
                manifest_digest=decision.identity_manifest_digest,
                target_manifest_digest=decision.target_manifest_digest,
                target_version=request.target_version,
                resource_path=request.resource_path,
                issued_at=now,
                expires_at=min(now + ttl_seconds, self._key_ring.current.expires_at),
                nonce=secrets.token_hex(16),
                key_id=self._key_ring.current.key_id,
            )
            payload = _canonical(claims)
            signature = hmac.new(
                self._key_ring.current.secret,
                payload,
                hashlib.sha256,
            ).digest()
            token = f"{_b64encode(payload)}.{_b64encode(signature)}"
            if len(token) > self._policy.maximum_token_bytes:
                raise permission_denied()
            return token

    def authenticate_token(self, token: str) -> CapabilityTokenClaims:
        now = int(time.time())
        if not isinstance(token, str):
            raise permission_denied()
        try:
            if len(token.encode("utf-8")) > self._policy.maximum_token_bytes:
                raise permission_denied()
        except UnicodeError as exc:
            raise permission_denied() from exc
        parts = token.split(".")
        if len(parts) != 2:
            raise permission_denied()
        payload = _b64decode(parts[0])
        signature = _b64decode(parts[1])
        try:
            raw = json.loads(payload.decode("utf-8"))
            claims = CapabilityTokenClaims(**raw)
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise permission_denied() from exc
        if (
            not isinstance(claims.version, int)
            or isinstance(claims.version, bool)
            or claims.version != self._authority.authority_version_policy.current_version
        ):
            raise permission_denied()
        text_values = (
            claims.issuer,
            claims.audience,
            claims.identity_group,
            claims.actor,
            claims.bound_tool_id,
            claims.capability,
            claims.action,
            claims.target,
            claims.data_scope,
            claims.caller_path,
            claims.manifest_digest,
            claims.nonce,
            claims.key_id,
        )
        optional_text_values = (
            claims.target_tool_id,
            claims.target_manifest_digest,
            claims.target_version,
            claims.resource_path,
        )
        if any(not isinstance(value, str) or not value for value in text_values):
            raise permission_denied()
        if any(
            value is not None and (not isinstance(value, str) or not value)
            for value in optional_text_values
        ):
            raise permission_denied()
        if (
            not isinstance(claims.issued_at, int)
            or isinstance(claims.issued_at, bool)
            or not isinstance(claims.expires_at, int)
            or isinstance(claims.expires_at, bool)
            or claims.expires_at <= claims.issued_at
            or claims.expires_at - claims.issued_at
            > self._policy.maximum_token_ttl_seconds
            or len(claims.nonce) < self._policy.minimum_nonce_characters
        ):
            raise permission_denied()
        governance = governance_policy_snapshot()
        if (
            claims.issuer != governance.identity_authentication.issuer
            or claims.audience != governance.identity_authentication.audience
            or claims.identity_group != self._actor_identity_group()
            or claims.actor != self._actor
            or claims.bound_tool_id != self._bound_tool_id
            or claims.caller_path != self._caller_path
        ):
            raise permission_denied()
        if now < claims.issued_at - self._policy.allowed_clock_skew_seconds:
            raise permission_denied()
        if now >= claims.expires_at + self._policy.allowed_clock_skew_seconds:
            raise permission_denied()
        with self._lock:
            self._assert_process_binding()
            self.verify_runtime_integrity()
            key = self._resolve_key(claims.key_id, now)
            expected = hmac.new(key.secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise permission_denied()
            if payload != _canonical(claims):
                raise permission_denied()
            request = PermissionRequest(
                actor=claims.actor,
                bound_tool_id=claims.bound_tool_id,
                target_tool_id=claims.target_tool_id,
                capability=claims.capability,
                action=claims.action,
                target=claims.target,
                data_scope=claims.data_scope,
                caller_path=claims.caller_path,
                target_version=claims.target_version,
                resource_path=claims.resource_path,
            )
            decision = authorize_permission_request(request, self._project_root)
            verify_manifest_digests(
                decision,
                claims.manifest_digest,
                claims.target_manifest_digest,
            )
            self._nonces.consume(
                "capability-token",
                claims.actor,
                claims.nonce,
                claims.expires_at,
                now,
            )
            return claims


__all__ = ["TokenOperationsMixin"]
