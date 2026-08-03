from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance_rule.execution import authentication as authentication_module  # noqa: E402
from governance_rule.execution.authentication import (  # noqa: E402
    GovernanceAuthenticationService,
    sign_launcher_attestation,
)
from governance_rule.execution.integrity import (  # noqa: E402
    AuthorityIntegrityGuard,
    build_integrity_manifest,
)
from governance_rule.governance_policy import governance_policy_snapshot  # noqa: E402
from governance_rule.permission_directory.directory_authority import (  # noqa: E402
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution import path_guard  # noqa: E402


@pytest.fixture(autouse=True)
def _bind_isolated_canonical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        path_guard,
        "GPTBRIDGE_PROJECT_ROOT",
        str(tmp_path / "project"),
    )


def _protected_sources() -> tuple[str, ...]:
    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    return (*policy.authority_files, *directory.managed_read_only_registry_paths)


def _isolated_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    for relative in _protected_sources():
        source = ROOT / relative
        destination = project_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(stat.S_IREAD | stat.S_IWRITE)
    (project_root / "main-system").mkdir(exist_ok=True)
    return project_root


def _launcher_material(project_root: Path, *, process_id: int | None = None):
    issued_at = int(time.time())
    launcher_key = secrets.token_bytes(32)
    key_id = secrets.token_hex(16)
    manifest = build_integrity_manifest(
        project_root,
        launcher_key,
        issued_at=issued_at,
        key_id=key_id,
    )
    attestation = sign_launcher_attestation(
        launcher_key,
        actor="governance/main-system",
        bound_tool_id="main-system",
        caller_path="main-system",
        process_id=os.getppid() if process_id is None else process_id,
        issued_at=issued_at,
        key_id=key_id,
    )
    return launcher_key, manifest, attestation


def _authenticator(project_root: Path) -> GovernanceAuthenticationService:
    launcher_key, manifest, attestation = _launcher_material(project_root)
    return GovernanceAuthenticationService(
        project_root,
        launcher_key,
        manifest,
        attestation,
    )


def _governance_token(authentication: GovernanceAuthenticationService, **options: int) -> str:
    return authentication.issue_token(
        capability="governance",
        action="enforce",
        target="governed-operation",
        data_scope="none",
        **options,
    )


def _tamper_claim(token: str, field: str, value: object) -> str:
    payload, signature = token.split(".")
    padding = "=" * (-len(payload) % 4)
    claims = json.loads(
        base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    )
    claims[field] = value
    tampered_payload = base64.urlsafe_b64encode(
        json.dumps(
            claims,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"{tampered_payload}.{signature}"


def test_integrity_guard_rejects_tampered_enforcement_source(tmp_path: Path) -> None:
    project_root = _isolated_project(tmp_path)
    launcher_key, manifest, attestation = _launcher_material(project_root)
    guard = AuthorityIntegrityGuard(
        project_root,
        launcher_key,
        manifest,
        expected_key_id=attestation.key_id,
    )
    protected_target = (
        project_root
        / "governance_rule"
        / "execution"
        / "authentication"
        / "__init__.py"
    )
    protected_target.write_text(
        protected_target.read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        guard.verify()


def test_authenticator_rejects_tampered_capability_token(tmp_path: Path) -> None:
    authentication = _authenticator(_isolated_project(tmp_path))
    try:
        payload, signature = _governance_token(authentication).split(".")
        replacement = "A" if signature[0] != "A" else "B"
        tampered = f"{payload}.{replacement}{signature[1:]}"

        with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
            authentication.authenticate_token(tampered)
    finally:
        authentication.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("issued_at", "invalid"),
        ("expires_at", None),
        ("key_id", []),
        ("nonce", 1),
        ("capability", None),
        ("target_tool_id", 1),
    ),
)
def test_authenticator_rejects_malformed_claim_types(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    authentication = _authenticator(_isolated_project(tmp_path))
    try:
        malformed = _tamper_claim(_governance_token(authentication), field, value)

        with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
            authentication.authenticate_token(malformed)
    finally:
        authentication.close()


@pytest.mark.parametrize("issued_at", (True, "1"))
def test_launcher_signing_rejects_invalid_issue_time(issued_at: object) -> None:
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        sign_launcher_attestation(
            secrets.token_bytes(32),
            actor="governance/main-system",
            bound_tool_id="main-system",
            caller_path="main-system",
            process_id=os.getpid(),
            issued_at=issued_at,  # type: ignore[arg-type]
            key_id=secrets.token_hex(16),
        )


def test_authenticator_rejects_replayed_capability_token(tmp_path: Path) -> None:
    authentication = _authenticator(_isolated_project(tmp_path))
    try:
        token = _governance_token(authentication)
        authentication.authenticate_token(token)

        with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
            authentication.authenticate_token(token)
    finally:
        authentication.close()


def test_authenticator_rejects_expired_capability_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authentication = _authenticator(_isolated_project(tmp_path))
    try:
        issued_at = int(time.time())
        token = _governance_token(authentication, ttl_seconds=1)
        monkeypatch.setattr(authentication_module.time, "time", lambda: issued_at + 32)

        with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
            authentication.authenticate_token(token)
    finally:
        authentication.close()


def test_authenticator_rejects_non_ancestor_process_binding(tmp_path: Path) -> None:
    project_root = _isolated_project(tmp_path)
    launcher_key, manifest, attestation = _launcher_material(
        project_root,
        process_id=os.getpid(),
    )

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        GovernanceAuthenticationService(
            project_root,
            launcher_key,
            manifest,
            attestation,
        )


def test_authenticator_rejects_tampered_launcher_signature(tmp_path: Path) -> None:
    project_root = _isolated_project(tmp_path)
    launcher_key, manifest, attestation = _launcher_material(project_root)
    tampered_attestation = replace(attestation, signature="0" * 64)

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        GovernanceAuthenticationService(
            project_root,
            launcher_key,
            manifest,
            tampered_attestation,
        )
