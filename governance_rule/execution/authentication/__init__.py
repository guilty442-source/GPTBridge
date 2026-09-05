from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.execution.integrity import (
    AuthorityIntegrityGuard,
    AuthorityIntegrityManifest,
)
from governance_rule.execution.versioning import (
    validate_loaded_authority_version,
)
from governance_rule.permission_directory.execution.identity_registry import (
    AuthorizationDecision,
    PermissionRequest,
    authorize_permission_request,
    verify_manifest_digests,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
    resolve_project_path,
)
from governance_rule.governance_policy import governance_policy_snapshot


def _windows_parent_process_id(process_id: int) -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return None
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if int(entry.th32ProcessID) == process_id:
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


def _is_launcher_ancestor(process_id: int, *, maximum_depth: int = 4) -> bool:
    if process_id <= 0:
        return False
    if os.name != "nt":
        return os.getppid() == process_id
    current = os.getpid()
    for _ in range(maximum_depth):
        parent = _windows_parent_process_id(current)
        if parent is None or parent <= 0:
            return False
        if parent == process_id:
            return True
        current = parent
    return False


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


def _canonical(value: object, *, omit_signature: bool = False) -> bytes:
    data = asdict(value)
    if omit_signature:
        data.pop("signature", None)
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError) as exc:
        raise permission_denied() from exc


def sign_launcher_attestation(
    launcher_key: bytes,
    *,
    actor: str,
    bound_tool_id: str,
    caller_path: str,
    process_id: int,
    issued_at: int,
    key_id: str,
) -> LauncherIdentityAttestation:
    policy = directory_authority_snapshot().key_management_policy
    if not isinstance(launcher_key, bytes) or len(launcher_key) < policy.minimum_key_bytes:
        raise permission_denied()
    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        raise permission_denied()
    if not isinstance(issued_at, int) or isinstance(issued_at, bool):
        raise permission_denied()
    values = (actor, bound_tool_id, caller_path, key_id)
    if any(not isinstance(value, str) or not value for value in values):
        raise permission_denied()
    unsigned = LauncherIdentityAttestation(
        issuer=policy.identity_attestation_issuer,
        actor=actor,
        bound_tool_id=bound_tool_id,
        caller_path=caller_path,
        process_id=process_id,
        issued_at=issued_at,
        expires_at=issued_at + policy.identity_attestation_ttl_seconds,
        nonce=secrets.token_hex(16),
        key_id=key_id,
        signature="",
    )
    signature = hmac.new(
        launcher_key,
        _canonical(unsigned, omit_signature=True),
        hashlib.sha256,
    ).hexdigest()
    return replace(unsigned, signature=signature)


class _NonceStore:
    def __init__(self, project_root: Path, policy: object) -> None:
        database = resolve_project_path(project_root, policy.nonce_store_path)
        directory = directory_authority_snapshot()
        main_boundary = next(
            (
                boundary
                for boundary in directory.boundaries
                if boundary.key == "main_system"
            ),
            None,
        )
        if main_boundary is None or len(main_boundary.runtime_writable_roots) != 1:
            raise permission_denied()
        runtime_root = resolve_project_path(
            project_root,
            main_boundary.runtime_writable_roots[0],
        )
        try:
            database.relative_to(runtime_root)
        except ValueError as exc:
            raise permission_denied() from exc
        database.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(database),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS governance_used_nonces ("
                "namespace TEXT NOT NULL, actor TEXT NOT NULL, "
                "nonce TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                "PRIMARY KEY (namespace, actor, nonce)) WITHOUT ROWID"
            )
        except sqlite3.Error as exc:
            raise permission_denied() from exc
        self._lock = threading.RLock()
        self._clock_skew = policy.allowed_clock_skew_seconds

    def consume(
        self,
        namespace: str,
        actor: str,
        nonce: str,
        expires_at: int,
        now: int,
    ) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "DELETE FROM governance_used_nonces WHERE expires_at < ?",
                    (now - self._clock_skew,),
                )
                self._connection.execute(
                    "INSERT INTO governance_used_nonces "
                    "(namespace, actor, nonce, expires_at) VALUES (?, ?, ?, ?)",
                    (namespace, actor, nonce, expires_at),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise permission_denied() from exc
            except sqlite3.Error as exc:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise permission_denied() from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _new_key(policy: object, now: int) -> _SigningKey:
    return _SigningKey(
        key_id=secrets.token_hex(16),
        secret=secrets.token_bytes(policy.minimum_key_bytes),
        issued_at=now,
        expires_at=now + policy.maximum_key_lifetime_seconds,
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
            self._integrity.verify()
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
        """Verify that this process is still bound to the current authority files."""

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
            self._integrity.verify()
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
