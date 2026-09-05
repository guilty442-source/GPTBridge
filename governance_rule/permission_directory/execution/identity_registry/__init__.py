from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path

from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
    resolve_project_path,
    validate_grant_resource_path,
)
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)


@dataclass(frozen=True)
class PermissionRequest:
    actor: str
    bound_tool_id: str
    target_tool_id: str | None
    capability: str
    action: str
    target: str
    data_scope: str
    caller_path: str
    target_version: str | None
    resource_path: str | None


@dataclass(frozen=True)
class AuthorizationDecision:
    identity_manifest_digest: str
    target_manifest_digest: str | None
    current_code_version: str


def _validate_request_types(request: PermissionRequest) -> None:
    required = (
        request.actor,
        request.bound_tool_id,
        request.capability,
        request.action,
        request.target,
        request.data_scope,
        request.caller_path,
    )
    if any(not isinstance(value, str) or not value for value in required):
        raise permission_denied()
    for value in (
        request.target_tool_id,
        request.target_version,
        request.resource_path,
    ):
        if value is not None and (
            not isinstance(value, str) or not value
        ):
            raise permission_denied()


def _identity_for_request(
    authority: object,
    identity_group: object,
    permission_bindings: tuple[object, ...],
    request: PermissionRequest,
) -> object:
    identity = None
    for candidate in identity_group.identities:
        uses_template = "{tool_id}" in candidate.actor
        if uses_template:
            if not re.fullmatch(authority.tool_id_pattern, request.bound_tool_id):
                continue
            actor = candidate.actor.format(tool_id=request.bound_tool_id)
            tool_id = candidate.bound_tool_id.format(
                tool_id=request.bound_tool_id
            )
        else:
            actor = candidate.actor
            tool_id = candidate.bound_tool_id
        if request.actor == actor and request.bound_tool_id == tool_id:
            identity = candidate
            break
    if (
        identity is None
        or identity.group_id not in authority.active_identity_group_ids
    ):
        raise permission_denied()
    binding = next(
        (
            item
            for item in permission_bindings
            if item.group_id == identity.group_id
            and item.actor == identity.actor
        ),
        None,
    )
    if binding is None or request.capability not in binding.capabilities:
        raise permission_denied()
    return identity


def _read_manifest(
    identity: object,
    project_root: Path,
    tool_id: str,
) -> tuple[str, str]:
    binding = identity.manifest_binding
    if not binding.required:
        from governance_rule.governance_policy import governance_policy_snapshot

        return "built-in", str(governance_policy_snapshot().authority_version)
    path = resolve_project_path(
        project_root,
        binding.path_template.format(tool_id=tool_id),
    )
    if not path.is_file():
        raise permission_denied()
    try:
        with path.open("rb") as stream:
            content = stream.read(binding.maximum_bytes + 1)
    except OSError as exc:
        raise permission_denied() from exc
    if not content or len(content) > binding.maximum_bytes:
        raise permission_denied()
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise permission_denied() from exc
    if not isinstance(manifest, dict):
        raise permission_denied()
    if manifest.get(binding.tool_id_field) != tool_id:
        raise permission_denied()
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise permission_denied()
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        raise permission_denied()
    if any(name not in capabilities for name in binding.required_capabilities):
        raise permission_denied()
    for requirement in binding.requirements:
        value: object = manifest
        for field in requirement.field_path:
            if not isinstance(value, dict) or field not in value:
                raise permission_denied()
            value = value[field]
        if value != requirement.expected_value:
            raise permission_denied()
    return hashlib.sha256(content).hexdigest(), version


def _numeric_version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)*",
        value,
    ):
        raise permission_denied()
    return tuple(int(item) for item in value.split("."))


def _validate_version(
    current_version: str,
    target_version: str | None,
    transition: str,
) -> None:
    if transition == "none":
        if target_version is not None:
            raise permission_denied()
        return
    if transition != "increment" or target_version is None:
        raise permission_denied()
    current = _numeric_version(current_version)
    target = _numeric_version(target_version)
    width = max(len(current), len(target))
    current += (0,) * (width - len(current))
    target += (0,) * (width - len(target))
    if target <= current:
        raise permission_denied()


def authorize_permission_request(
    request: PermissionRequest,
    project_root: Path,
) -> AuthorizationDecision:
    _validate_request_types(request)
    authority = directory_authority_snapshot()
    identity_group = identity_group_snapshot()
    if identity_group.group_id != authority.active_identity_group_id:
        raise permission_denied()
    permission_bindings = identity_permission_snapshot()
    capability_authorities, _ = capability_boundary_snapshot()
    identity = _identity_for_request(
        authority,
        identity_group,
        permission_bindings,
        request,
    )
    caller = resolve_project_path(project_root, request.caller_path)
    if not caller.exists():
        raise permission_denied()
    if not any(
        _is_within(
            caller,
            resolve_project_path(
                project_root,
                template.format(tool_id=request.bound_tool_id),
            ),
        )
        for template in identity.bound_roots
    ):
        raise permission_denied()

    identity_digest, current_version = _read_manifest(
        identity,
        project_root,
        request.bound_tool_id,
    )
    operation_tool_id = request.bound_tool_id
    target_digest = None
    if request.target_tool_id is not None:
        if "{tool_id}" in identity.actor:
            raise permission_denied()
        if not re.fullmatch(authority.tool_id_pattern, request.target_tool_id):
            raise permission_denied()
        operation_tool_id = request.target_tool_id
        target_identity = next(
            (
                item
                for item in identity_group.identities
                if (
                    "{tool_id}" in item.actor
                    or item.bound_tool_id == request.target_tool_id
                )
            ),
            None,
        )
        if target_identity is None:
            raise permission_denied()
        target_digest, current_version = _read_manifest(
            target_identity,
            project_root,
            operation_tool_id,
        )

    capability = next(
        (
            item
            for item in capability_authorities
            if item.capability == request.capability
        ),
        None,
    )
    if capability is None:
        raise permission_denied()
    owner = capability.owner.format(tool_id=request.bound_tool_id)
    if owner == "main-system":
        owner_matches = request.bound_tool_id == "main-system"
    elif owner == "governance-policy":
        owner_matches = True
    elif owner.startswith("tool:"):
        owner_matches = request.bound_tool_id == owner.removeprefix("tool:")
    else:
        owner_matches = False
    if not owner_matches:
        raise permission_denied()

    grant = next(
        (
            item
            for item in capability.grants
            if request.action == item.action
            and request.target
            == item.target.format(tool_id=operation_tool_id)
            and request.data_scope
            == item.data_scope.format(tool_id=operation_tool_id)
        ),
        None,
    )
    if grant is None:
        raise permission_denied()
    uses_tool_target = any(
        "{tool_id}" in value
        for value in (
            grant.target,
            grant.data_scope,
            *grant.path_roots,
            *grant.excluded_path_roots,
        )
    )
    if identity.bound_tool_id == "main-system":
        # The main system may use a tool-scoped grant only when it names an
        # explicitly registered target identity.
        if uses_tool_target != (request.target_tool_id is not None):
            raise permission_denied()
    elif request.target_tool_id is not None:
        # Independent tools may name another registered tool only through the
        # governance-owned shared request channel. No target storage is
        # exposed by this exception.
        if request.capability not in {
            "system-channel-request-submit",
            "ai-channel-request-submit",
        }:
            raise permission_denied()
    _validate_version(
        current_version,
        request.target_version,
        grant.version_transition,
    )
    validate_grant_resource_path(
        project_root,
        request.resource_path,
        grant,
        operation_tool_id,
    )
    return AuthorizationDecision(
        identity_manifest_digest=identity_digest,
        target_manifest_digest=target_digest,
        current_code_version=current_version,
    )


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def verify_manifest_digests(
    expected: AuthorizationDecision,
    identity_digest: str,
    target_digest: str | None,
) -> None:
    if not hmac.compare_digest(
        expected.identity_manifest_digest,
        identity_digest,
    ):
        raise permission_denied()
    if expected.target_manifest_digest is None:
        if target_digest is not None:
            raise permission_denied()
    elif target_digest is None or not hmac.compare_digest(
        expected.target_manifest_digest,
        target_digest,
    ):
        raise permission_denied()
