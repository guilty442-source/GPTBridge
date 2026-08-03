from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.authentication import (
    CapabilityTokenClaims,
    GovernanceAuthenticationService,
    LauncherIdentityAttestation,
)
from governance_rule.execution.integrity import AuthorityIntegrityManifest
from governance_rule.execution.integrity import build_integrity_manifest
from governance_rule.execution.authentication import sign_launcher_attestation
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from shared_layer import SharedLayerStore


GOVERNANCE_BOOTSTRAP_ENV: Final[str] = "GPTBRIDGE_GOVERNANCE_BOOTSTRAP"
_MAXIMUM_BOOTSTRAP_BYTES: Final[int] = 16_384
_BOOTSTRAP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "format_version",
        "launcher_key",
        "integrity_manifest",
        "identity_attestation",
    }
)


def _permission_denied() -> PermissionError:
    return PermissionError("PERMISSION_DENIED")


def _required_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _permission_denied()
    return value


def _load_integrity_manifest(value: object) -> AuthorityIntegrityManifest:
    raw = _required_mapping(value)
    if set(raw) != {
        "authority_version",
        "file_digests",
        "issued_at",
        "key_id",
        "signature",
    }:
        raise _permission_denied()
    digest_rows = raw.get("file_digests")
    if not isinstance(digest_rows, list):
        raise _permission_denied()
    digests: list[tuple[str, str]] = []
    for row in digest_rows:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(item, str) and item for item in row)
        ):
            raise _permission_denied()
        digests.append((row[0], row[1]))
    try:
        return AuthorityIntegrityManifest(
            authority_version=raw["authority_version"],
            file_digests=tuple(digests),
            issued_at=raw["issued_at"],
            key_id=raw["key_id"],
            signature=raw["signature"],
        )
    except (KeyError, TypeError) as exc:
        raise _permission_denied() from exc


def _load_identity_attestation(value: object) -> LauncherIdentityAttestation:
    raw = _required_mapping(value)
    expected = {
        "issuer",
        "actor",
        "bound_tool_id",
        "caller_path",
        "process_id",
        "issued_at",
        "expires_at",
        "nonce",
        "key_id",
        "signature",
    }
    if set(raw) != expected:
        raise _permission_denied()
    try:
        return LauncherIdentityAttestation(**raw)
    except TypeError as exc:
        raise _permission_denied() from exc


class MainSystemGovernance:
    """The only main-system gateway to governed privileged operations."""

    def __init__(
        self,
        project_root: Path,
        authentication: GovernanceAuthenticationService,
    ) -> None:
        self._project_root = project_root
        self._authentication = authentication
        self._integrity_ready = False
        self._integrity_checked_at = 0.0
        self.authorize(
            capability="governance",
            action="enforce",
            target="governed-operation",
            data_scope="none",
        )
        self._integrity_ready = True
        self._integrity_checked_at = time.monotonic()

    @classmethod
    def from_environment(cls, project_root: Path | str) -> MainSystemGovernance:
        encoded = os.environ.pop(GOVERNANCE_BOOTSTRAP_ENV, None)
        if not isinstance(encoded, str) or not encoded:
            raise _permission_denied()
        try:
            if len(encoded.encode("ascii")) > _MAXIMUM_BOOTSTRAP_BYTES:
                raise _permission_denied()
            decoded = base64.b64decode(encoded, validate=True)
            payload = json.loads(decoded.decode("utf-8"))
        except (UnicodeError, ValueError, binascii.Error, json.JSONDecodeError) as exc:
            raise _permission_denied() from exc
        if not isinstance(payload, dict) or set(payload) != _BOOTSTRAP_KEYS:
            raise _permission_denied()
        if payload.get("format_version") != 1:
            raise _permission_denied()
        key_text = payload.get("launcher_key")
        if not isinstance(key_text, str) or not key_text:
            raise _permission_denied()
        try:
            launcher_key = base64.b64decode(key_text, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise _permission_denied() from exc
        manifest = _load_integrity_manifest(payload.get("integrity_manifest"))
        attestation = _load_identity_attestation(payload.get("identity_attestation"))
        root = Path(project_root).resolve()
        authentication = GovernanceAuthenticationService(
            root,
            launcher_key,
            manifest,
            attestation,
        )
        return cls(root, authentication)

    def authorize(
        self,
        *,
        capability: str,
        action: str,
        target: str,
        data_scope: str,
        target_tool_id: str | None = None,
        target_version: str | None = None,
        resource_path: str | None = None,
    ) -> CapabilityTokenClaims:
        token = self._authentication.issue_token(
            target_tool_id=target_tool_id,
            capability=capability,
            action=action,
            target=target,
            data_scope=data_scope,
            target_version=target_version,
            resource_path=resource_path,
        )
        return self._authentication.authenticate_token(token)

    def authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        if tool_id == "governance_rule":
            raise _permission_denied()
        self.authorize(
            target_tool_id=tool_id,
            capability="independent-tool-start-and-stop",
            action=action,
            target=f"tool-process:{tool_id}",
            data_scope="none",
        )

    def create_tool_governance_bootstrap(self, tool_id: str) -> str:
        self.authorize_tool_lifecycle(tool_id, "start")
        identity = next(
            (
                item
                for item in identity_group_snapshot().identities
                if item.bound_tool_id == tool_id
            ),
            None,
        )
        if identity is None or not identity.bound_roots:
            raise _permission_denied()
        caller_path = identity.bound_roots[0].format(tool_id=tool_id)
        launcher_key = secrets.token_bytes(32)
        try:
            issued_at = int(time.time())
            key_id = secrets.token_hex(16)
            integrity = build_integrity_manifest(
                self._project_root,
                launcher_key,
                issued_at=issued_at,
                key_id=key_id,
            )
            attestation = sign_launcher_attestation(
                launcher_key,
                actor=f"governance/tool/{tool_id}",
                bound_tool_id=tool_id,
                caller_path=caller_path,
                process_id=os.getpid(),
                issued_at=issued_at,
                key_id=key_id,
            )
            payload = {
                "format_version": 1,
                "launcher_key": base64.b64encode(launcher_key).decode("ascii"),
                "integrity_manifest": asdict(integrity),
                "identity_attestation": asdict(attestation),
            }
            return base64.b64encode(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
        finally:
            launcher_key = b""

    def submit_tool_execution_request(
        self,
        tool_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> None:
        token = self._authentication.issue_token(
            target_tool_id=tool_id,
            capability="system-channel-request-submit",
            action="request",
            target=f"shared-layer-system-request:{tool_id}",
            data_scope="shared-layer-system-request",
            resource_path=(
                "postgresql:gptbridge_transport:system"
            ),
        )
        SharedLayerStore(self._project_root, self._authentication, "system").submit_request(
            token,
            request_id,
            tool_id,
            payload,
        )

    def cancel_tool_execution_request(
        self,
        tool_id: str,
        request_id: str,
    ) -> bool:
        token = self._authentication.issue_token(
            target_tool_id=tool_id,
            capability="system-channel-request-submit",
            action="cancel-request",
            target=f"shared-layer-system-request:{tool_id}",
            data_scope="shared-layer-system-request",
            resource_path=(
                "postgresql:gptbridge_transport:system"
            ),
        )
        return SharedLayerStore(
            self._project_root,
            self._authentication,
            "system",
        ).cancel_request(token, request_id, tool_id)

    def tool_execution_response(
        self,
        tool_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        token = self._authentication.issue_token(
            target_tool_id=tool_id,
            capability="system-channel-request-submit",
            action="consume-response",
            target=f"shared-layer-system-request:{tool_id}",
            data_scope="shared-layer-system-request",
            resource_path=(
                "postgresql:gptbridge_transport:system"
            ),
        )
        return SharedLayerStore(
            self._project_root,
            self._authentication,
            "system",
        ).consume_response(token, request_id, tool_id)

    @property
    def independent_tool_permissions_enabled(self) -> bool:
        return any(
            binding.actor == "governance/main-system"
            and "independent-tool-start-and-stop" in binding.capabilities
            for binding in identity_permission_snapshot()
        )

    def runtime_integrity_ready(self, *, max_age_seconds: float = 2.0) -> bool:
        """Return whether the launch credential still matches live authority files."""

        now = time.monotonic()
        if now - self._integrity_checked_at < max(0.0, max_age_seconds):
            return self._integrity_ready
        try:
            self._authentication.verify_runtime_integrity()
        except PermissionError:
            self._integrity_ready = False
        else:
            self._integrity_ready = True
        self._integrity_checked_at = now
        return self._integrity_ready

    def can_start_tool(self, tool_id: str) -> bool:
        try:
            self.authorize_tool_lifecycle(tool_id, "start")
        except PermissionError:
            return False
        return True

    def authorize_hot_update(
        self,
        tool_id: str,
        action: str,
        target_version: str,
        resource_path: str,
    ) -> None:
        self.authorize(
            target_tool_id=tool_id,
            capability="hot-update",
            action=action,
            target=f"tool-code:{tool_id}",
            data_scope="tool-code",
            target_version=target_version,
            resource_path=resource_path,
        )

    def close(self) -> None:
        self._authentication.close()
