from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
    resolve_project_path,
)
from governance_rule.governance_policy import governance_policy_snapshot


@dataclass(frozen=True)
class AuthorityIntegrityManifest:
    authority_version: int
    file_digests: tuple[tuple[str, str], ...]
    issued_at: int
    key_id: str
    signature: str


def _payload(manifest: AuthorityIntegrityManifest) -> bytes:
    values = asdict(manifest)
    values.pop("signature")
    return json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def build_integrity_manifest(
    project_root: Path,
    launcher_key: bytes,
    *,
    issued_at: int,
    key_id: str,
) -> AuthorityIntegrityManifest:
    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    key_policy = directory.key_management_policy
    if (
        not isinstance(launcher_key, bytes)
        or len(launcher_key) < key_policy.minimum_key_bytes
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(key_id, str)
        or not key_id
    ):
        raise permission_denied()
    digests = tuple(
        (
            relative,
            hashlib.sha256(
                resolve_project_path(project_root, relative).read_bytes()
            ).hexdigest(),
        )
        for relative in (
            *policy.authority_files,
            *directory.managed_read_only_registry_paths,
        )
    )
    unsigned = AuthorityIntegrityManifest(
        authority_version=policy.authority_version,
        file_digests=digests,
        issued_at=issued_at,
        key_id=key_id,
        signature="",
    )
    signature = hmac.new(
        launcher_key,
        _payload(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return AuthorityIntegrityManifest(
        authority_version=unsigned.authority_version,
        file_digests=unsigned.file_digests,
        issued_at=unsigned.issued_at,
        key_id=unsigned.key_id,
        signature=signature,
    )


class AuthorityIntegrityGuard:
    def __init__(
        self,
        project_root: Path,
        launcher_key: bytes,
        manifest: AuthorityIntegrityManifest,
        *,
        expected_key_id: str,
    ) -> None:
        policy = governance_policy_snapshot()
        directory = directory_authority_snapshot()
        key_policy = directory.key_management_policy
        if (
            not isinstance(launcher_key, bytes)
            or len(launcher_key) < key_policy.minimum_key_bytes
            or not isinstance(manifest, AuthorityIntegrityManifest)
            or not isinstance(expected_key_id, str)
            or not expected_key_id
            or manifest.key_id != expected_key_id
            or not isinstance(manifest.issued_at, int)
            or isinstance(manifest.issued_at, bool)
            or not isinstance(manifest.signature, str)
        ):
            raise permission_denied()
        now = int(time.time())
        if now < manifest.issued_at - key_policy.allowed_clock_skew_seconds:
            raise permission_denied()
        if now >= (
            manifest.issued_at
            + key_policy.maximum_key_lifetime_seconds
            + key_policy.allowed_clock_skew_seconds
        ):
            raise permission_denied()
        expected = hmac.new(
            launcher_key,
            _payload(manifest),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(manifest.signature, expected):
            raise permission_denied()
        if manifest.authority_version != policy.authority_version:
            raise permission_denied()
        protected_sources = (
            *policy.authority_files,
            *directory.managed_read_only_registry_paths,
        )
        if tuple(path for path, _digest in manifest.file_digests) != protected_sources:
            raise permission_denied()
        self._project_root = project_root
        self._manifest = manifest
        self.verify()

    def verify(self) -> None:
        for relative, expected_digest in self._manifest.file_digests:
            path = resolve_project_path(self._project_root, relative)
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise permission_denied() from exc
            actual = hashlib.sha256(content).hexdigest()
            if not hmac.compare_digest(actual, expected_digest):
                raise permission_denied()
