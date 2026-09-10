"""Process-bound authenticator created only from launcher-signed material."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from governance_rule.execution.integrity import (
    AuthorityIntegrityGuard,
    AuthorityIntegrityManifest,
)
from governance_rule.execution.versioning import (
    validate_loaded_authority_version,
)
from governance_rule.governance_policy import governance_policy_snapshot
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution.identity_registry import (
    AuthorizationDecision,
    PermissionRequest,
    authorize_permission_request,
    verify_manifest_digests,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)

from .auth_models import (
    CapabilityTokenClaims,
    LauncherIdentityAttestation,
    _KeyRing,
    _SigningKey,
)
from .auth_signing import (
    _b64decode,
    _b64encode,
    _canonical,
    _is_launcher_ancestor,
    _new_key,
    _NonceStore,
    sign_launcher_attestation,
)

__all__ = (
    "CapabilityTokenClaims",
    "GovernanceAuthenticationService",
    "LauncherIdentityAttestation",
    "sign_launcher_attestation",
)


class GovernanceAuthenticationService:
    """Process-bound authenticator created only from launcher-signed material."""

    def __init__(
        self,
        project_root: Path | str,
        launcher_key: bytes,
        integrity_manifest: AuthorityIntegrityManifest,
        attestation: LauncherIdentityAttestation,
    ) -> None:
        now = int(time.time())
        root = Path(project_root).resolve()
        authority = directory_authority_snapshot()
        policy = authority.key_management_policy
        if not root.is_dir():
            raise permission_denied()
        if (
            not isinstance(launcher_key, bytes)
            or len(launcher_key) < policy.minimum_key_bytes
        ):
            raise permission_denied()
        validate_loaded_authority_version()
        self._project_root = root
        self._authority = authority
        self._policy = policy
        self._lock = threading.RLock()
        self._closed = False
        self._process_id = os.getpid()
        self._launcher_key = launcher_key
        self._launcher_key_id = attestation.key_id
        self._launcher_process_id = (
            attestation.process_id
            if isinstance(attestation.process_id, int)
            and not isinstance(attestation.process_id, bool)
            else -1
        )
        self._verify_attestation(attestation, launcher_key, now)
        self._integrity = AuthorityIntegrityGuard(
            root,
            launcher_key,
            integrity_manifest,
            expected_key_id=attestation.key_id,
        )
        self._actor = attestation.actor
        self._bound_tool_id = attestation.bound_tool_id
        self._caller_path = attestation.caller_path
        self._key_ring = _KeyRing(_new_key(policy, now), None)
        self._nonces = _NonceStore(root, policy)
        self._nonces.consume(
            "launcher-attestation",
            attestation.actor,
            attestation.nonce,
            attestation.expires_at,
            now,
        )

    def _actor_identity_group(self) -> str:
        """Resolve this actor's dedicated identity group.

        Every registered identity owns exactly one group; a token minted for
        one tool must carry that tool's group and can never be replayed as a
        member of another tool's group (anti-jailbreak isolation).
        """

        for identity in identity_group_snapshot().identities:
            if (
                identity.actor == self._actor
                and identity.bound_tool_id == self._bound_tool_id
                and identity.group_id
                in self._authority.active_identity_group_ids
            ):
                return identity.group_id
        raise permission_denied()

    def _verify_attestation(
        self,
        attestation: LauncherIdentityAttestation,
        launcher_key: bytes,
        now: int,
    ) -> None:
        if not isinstance(attestation, LauncherIdentityAttestation):
            raise permission_denied()
        text_values = (
            attestation.issuer,
            attestation.actor,
            attestation.bound_tool_id,
            attestation.caller_path,
            attestation.nonce,
            attestation.key_id,
            attestation.signature,
        )
        if any(not isinstance(value, str) or not value for value in text_values):
            raise permission_denied()
        if (
            not isinstance(attestation.issued_at, int)
            or isinstance(attestation.issued_at, bool)
            or not isinstance(attestation.expires_at, int)
            or isinstance(attestation.expires_at, bool)
            or len(attestation.nonce) < self._policy.minimum_nonce_characters
        ):
            raise permission_denied()
        if attestation.issuer != self._policy.identity_attestation_issuer:
            raise permission_denied()
        if (
            not isinstance(attestation.process_id, int)
            or isinstance(attestation.process_id, bool)
            or not _is_launcher_ancestor(attestation.process_id)
        ):
            raise permission_denied()
        if now < attestation.issued_at - self._policy.allowed_clock_skew_seconds:
            raise permission_denied()
        if now >= attestation.expires_at + self._policy.allowed_clock_skew_seconds:
            raise permission_denied()
        if (
            attestation.expires_at - attestation.issued_at
            != self._policy.identity_attestation_ttl_seconds
        ):
            raise permission_denied()
        expected = hmac.new(
            launcher_key,
            _canonical(attestation, omit_signature=True),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(attestation.signature, expected):
            raise permission_denied()

    def _assert_process_binding(self) -> None:
        if (
            self._closed
            or os.getpid() != self._process_id
            or not _is_launcher_ancestor(self._launcher_process_id)
        ):
            raise permission_denied()

    def _rotate_if_due(self, now: int) -> None:
        current = self._key_ring.current
        if now >= current.expires_at:
            self._key_ring = _KeyRing(_new_key(self._policy, now), None)
        elif now - current.issued_at >= self._policy.rotation_interval_seconds:
            self._key_ring = _KeyRing(_new_key(self._policy, now), current)

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

    def verify_runtime_integrity(self) -> None:
        """Verify that this process is still bound to the current authority files.

        Fail-closed (A11/A15): if any protected file digest no longer matches
        the launch-time manifest, raise PermissionError.  The caller must
        handle the failure (typically by restarting the process so a fresh
        manifest is built from the updated files).
        """

        with self._lock:
            self._assert_process_binding()
            self._integrity.verify()

    def _resolve_key(self, key_id: str, now: int) -> _SigningKey:
        for key in (self._key_ring.current, self._key_ring.previous):
            if key is not None and key.key_id == key_id:
                if now >= key.expires_at + self._policy.allowed_clock_skew_seconds:
                    raise permission_denied()
                return key
        raise permission_denied()

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

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._nonces.close()
            self._closed = True

    def __enter__(self) -> GovernanceAuthenticationService:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()
