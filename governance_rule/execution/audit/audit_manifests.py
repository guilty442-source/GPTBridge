"""Tool manifest validation checks for the governance audit."""

from __future__ import annotations

import json
import re
from pathlib import Path

from governance_rule.code_rule_directory import code_rule_directory_snapshot
from governance_rule.governance_policy import governance_policy_snapshot
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from .audit_manifests_helpers import (
    _validate_top_level_manifest,
    _validate_nested_manifest,
    _load_manifest,
)


def _manifest_retired(manifest: dict) -> bool:
    """A manifest is retired only through its explicit lifecycle marker.

    A533/A534: a retired tool is non-executable.  Its manifest may remain
    as lineage/audit evidence, but it is excluded from the active
    independent-tool parity and never validated as an active tool.
    """
    lifecycle = manifest.get("lifecycle")
    return (
        isinstance(lifecycle, dict)
        and str(lifecycle.get("status") or "").strip().casefold()
        == "retired"
    )


def _validate_retired_manifest(
    manifest_path: Path,
    manifest: dict,
    tool_id: str,
    errors: list[str],
) -> None:
    """Validate the non-executability invariants of a retired manifest.

    A retired manifest must stay disabled, non-stoppable, non-independent
    and not running; reactivation is a failure, never a warning.
    """
    if not tool_id:
        errors.append(f"retired tool manifest lacks an identifier: {manifest_path}")
        return
    if manifest.get("enabled") is not False:
        errors.append(f"retired tool must be disabled: {tool_id}")
    if manifest.get("main_system_independent_tool") is True:
        errors.append(f"retired tool must not be an independent tool: {tool_id}")
    lifecycle = manifest.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("stoppable") is not False:
        errors.append(f"retired tool must be non-stoppable: {tool_id}")
    if str(manifest.get("status") or "").strip().casefold() == "running":
        errors.append(f"retired tool must not be running: {tool_id}")


def check_tool_manifests(root: Path, errors: list[str]) -> tuple[set[str], set[str]]:
    """Verify all tool manifests and return (manifest_tool_ids, physical_owner_roots).

    Returns the set of discovered tool IDs and physical owner roots so the
    caller can cross-check against identity registrations.
    """
    policy = governance_policy_snapshot()
    code_rules = code_rule_directory_snapshot()
    label_policy = policy.identifier_labels

    manifest_tool_ids: set[str] = set()
    physical_owner_roots: set[str] = set()

    standalone_dir = root / "Standalone tools"
    depth4_manifests = sorted(
        p for p in root.glob("*/*/*/*/manifest.json")
        if not p.relative_to(root).parts[0].startswith(".")
    )

    # Pass 1: depth-1 manifests (direct children of project root)
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = _load_manifest(manifest_path, errors)
        if manifest is None:
            continue
        tool_id = str(manifest.get("id") or "")
        _validate_top_level_manifest(
            manifest_path, manifest, tool_id, label_policy, code_rules,
            errors, manifest_tool_ids, physical_owner_roots,
            check_governance_lifecycle=True,
        )

    # Pass 2: depth-2 manifests under "Standalone tools/" (independent tools)
    for manifest_path in sorted(standalone_dir.glob("*/manifest.json")):
        manifest = _load_manifest(manifest_path, errors)
        if manifest is None:
            continue
        tool_id = str(manifest.get("id") or "")
        if _manifest_retired(manifest):
            _validate_retired_manifest(manifest_path, manifest, tool_id, errors)
            continue
        _validate_top_level_manifest(
            manifest_path, manifest, tool_id, label_policy, code_rules,
            errors, manifest_tool_ids, physical_owner_roots,
        )

    # Pass 2b: depth-3 companion tools under "Standalone tools/*/"
    for manifest_path in sorted(standalone_dir.glob("*/*/manifest.json")):
        manifest = _load_manifest(manifest_path, errors)
        if manifest is None:
            continue
        tool_id = str(manifest.get("id") or "")
        if _manifest_retired(manifest):
            _validate_retired_manifest(manifest_path, manifest, tool_id, errors)
            continue
        _validate_nested_manifest(
            manifest_path, manifest, tool_id, label_policy, code_rules,
            errors, manifest_tool_ids, physical_owner_roots,
            expected_parent_name=manifest_path.parent.parent.name,
            depth_label="nested tool",
        )

    # Pass 2c: depth-4 companion tools under "Standalone tools/*/*/"
    for manifest_path in depth4_manifests:
        manifest = _load_manifest(manifest_path, errors)
        if manifest is None:
            continue
        tool_id = str(manifest.get("id") or "")
        if _manifest_retired(manifest):
            _validate_retired_manifest(manifest_path, manifest, tool_id, errors)
            continue
        _validate_nested_manifest(
            manifest_path, manifest, tool_id, label_policy, code_rules,
            errors, manifest_tool_ids, physical_owner_roots,
            expected_parent_name=manifest_path.parent.parent.parent.name,
            depth_label="depth-4 tool",
        )

    return manifest_tool_ids, physical_owner_roots

def _check_manifest_capabilities(
    manifest: dict,
    tool_id: str,
    code_rules: object,
    label_policy: object,
    errors: list[str],
) -> None:
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        errors.append(f"tool capabilities are missing: {tool_id}")
    else:
        for capability_name in capabilities:
            if (
                capability_name not in code_rules.approved_capability_names
                or re.fullmatch(
                    label_policy.capability_pattern,
                    capability_name,
                )
                is None
            ):
                errors.append(
                    f"tool capability label is not standardized: "
                    f"{tool_id}:{capability_name}"
                )


def _check_manifest_window(
    manifest: dict,
    tool_id: str,
    errors: list[str],
) -> None:
    window = manifest.get("window")
    if isinstance(window, dict) and (
        "title" in window
        or window.get("title_key") != "tool.window_title"
    ):
        errors.append(f"tool window label is not standardized: {tool_id}")


def _check_manifest_locale(
    manifest_path: Path,
    tool_id: str,
    code_rules: object,
    label_policy: object,
    errors: list[str],
) -> None:
    locale_path = manifest_path.parent / "locales" / "zh-TW.json"
    try:
        locale = json.loads(locale_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"traditional Chinese locale is invalid: {tool_id}: {error}")
        locale = {}
    if not isinstance(locale, dict) or not all(
        isinstance(key, str)
        and re.fullmatch(label_policy.locale_key_pattern, key)
        and isinstance(value, str)
        for key, value in locale.items()
    ):
        errors.append(f"traditional Chinese locale schema is invalid: {tool_id}")
    if not set(code_rules.required_locale_keys).issubset(locale):
        errors.append(f"traditional Chinese locale keys are incomplete: {tool_id}")


def _check_manifest_permissions(
    manifest: dict,
    tool_id: str,
    errors: list[str],
) -> None:
    permissions = manifest.get("permissions")
    if not isinstance(permissions, dict):
        errors.append(f"tool permissions are missing: {tool_id}")
        return
    expected_code_scope = (
        "project-source-excluding-governance-rule"
        if tool_id == "xingcheng"
        else "tool-root-only"
    )
    expected_database_scope = (
        "opaque-central-index-read-and-xingcheng-internal-read-write"
        if tool_id == "xingcheng"
        else "tool-database-only"
        if tool_id != "governance_rule"
        else "none"
    )
    if permissions.get("code_scope") != expected_code_scope:
        errors.append(f"tool code scope is invalid: {tool_id}")
    database_scope = permissions.get("database_scope")
    if database_scope != expected_database_scope:
        errors.append(f"tool database scope is invalid: {tool_id}")


def check_tool_identity_registration(
    root: Path,
    errors: list[str],
    manifest_tool_ids: set[str],
) -> None:
    """Verify manifest tool IDs match registered identities and approved names."""
    code_rules = code_rule_directory_snapshot()
    identity_group = identity_group_snapshot()

    # Infrastructure tools (governance_rule, shared-layer) are resident
    # services, not independent tools — they have identities but no
    # main_system_independent_tool manifest flag.  Companion tools
    # (e.g. star-chat) are nested under an independent tool and share
    # their owner's permission profile.  Exclude both classes from the
    # independent-tool identity parity check.
    _NON_INDEPENDENT_TOOL_IDS = frozenset({
        "governance_rule", "shared-layer", "star-chat",
    })
    # A533/A534: retired identities and manifests stay registered as
    # lineage/audit evidence but are excluded from the active parity
    # check — the approved list is compared on exactly the enabled tools.
    retired_tool_ids = {
        identity.bound_tool_id
        for identity in identity_group.identities
        if identity.lifecycle == "retired"
    }
    registered_tool_ids = {
        identity.bound_tool_id
        for identity in identity_group.identities
        if identity.bound_tool_id != "main-system"
        and identity.bound_tool_id not in _NON_INDEPENDENT_TOOL_IDS
        and identity.lifecycle != "retired"
    }
    manifest_independent_ids = (
        manifest_tool_ids - _NON_INDEPENDENT_TOOL_IDS - retired_tool_ids
    )
    if registered_tool_ids != manifest_independent_ids:
        errors.append("each independent tool must have exactly one enabled identity")
    approved_independent_ids = (
        set(code_rules.approved_tool_ids)
        - _NON_INDEPENDENT_TOOL_IDS
        - retired_tool_ids
    )
    if manifest_independent_ids != approved_independent_ids:
        errors.append("tool identifiers do not match the approved name list")
