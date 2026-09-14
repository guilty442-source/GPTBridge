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
from .auth_tokens import TokenOperationsMixin

__all__ = (
    "CapabilityTokenClaims",
    "GovernanceAuthenticationService",
    "LauncherIdentityAttestation",
    "sign_launcher_attestation",
)


class GovernanceAuthenticationService(TokenOperationsMixin):
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

    def reanchor_runtime_integrity(self) -> None:
        """Re-anchor this process to the live authority files (no restart).

        A governed authority update (codex or managed registry) changes file
        digests while the process keeps running.  Instead of forcing a
        restart, the launch manifest is rebuilt and re-signed with the same
        launcher key under the identical structural validation; an invalid
        or partially written update fails closed and the previous baseline
        remains in effect.  Called by the governed authority re-anchor
        service once the new authority has passed the governance audit.
        """

        with self._lock:
            self._assert_process_binding()
            self._integrity.reanchor()

    def _resolve_key(self, key_id: str, now: int) -> _SigningKey:
        for key in (self._key_ring.current, self._key_ring.previous):
            if key is not None and key.key_id == key_id:
                if now >= key.expires_at + self._policy.allowed_clock_skew_seconds:
                    raise permission_denied()
                return key
        raise permission_denied()

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
