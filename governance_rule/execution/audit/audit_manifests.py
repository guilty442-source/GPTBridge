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

    # A278/A280: independent tools live under "Standalone tools/".
    # Depth-1 manifests are direct children of the project root (e.g.
    # governance_rule, main-system).  Depth-2 manifests are direct
    # children of "Standalone tools/" (the independent-tools container).
    # Depth-3 manifests are companion tools nested under a tool root
    # (e.g. Standalone tools/local-model/model-dialogue).  Anything
    # deeper is forbidden.
    standalone_dir = root / "Standalone tools"
    # Exclude hidden directories (e.g. .kilo, .git) from the depth check.
    deep_manifests = sorted(
        p for p in root.glob("*/*/*/*/manifest.json")
        if not p.relative_to(root).parts[0].startswith(".")
    )
    if deep_manifests:
        errors.append(
            "nested independent tool folder exceeds allowed depth: "
            f"{[str(p.relative_to(root)) for p in deep_manifests]}"
        )

    # Pass 1: depth-1 manifests (direct children of project root)
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid tool manifest: {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        manifest_tool_ids.add(tool_id)
        physical_owner_root = str(manifest.get("physical_owner_root") or "")
        if physical_owner_root:
            physical_owner_roots.add(physical_owner_root)
        if (
            tool_id != manifest_path.parent.name
            and physical_owner_root != manifest_path.parent.name
        ):
            errors.append(f"tool identity mismatch: {manifest_path}")
        if re.fullmatch(label_policy.tool_id_pattern, tool_id) is None:
            errors.append(f"tool identifier is not standardized: {tool_id}")
        if "name" in manifest or manifest.get("name_key") != "tool.name":
            errors.append(f"tool name label is not standardized: {tool_id}")
        if tool_id == "governance_rule":
            expected_lifecycle = {
                "startup": "default-before-main-system",
                "directLoad": True,
                "encapsulated": False,
                "optional": False,
                "stoppable": False,
                "disableable": False,
                "unloadable": False,
            }
            if (
                manifest.get("status") != "running"
                or manifest.get("lifecycle") != expected_lifecycle
                or "executable" in manifest
            ):
                errors.append("governance manifest lifecycle is invalid")
        _check_manifest_capabilities(manifest, tool_id, code_rules, label_policy, errors)
        _check_manifest_window(manifest, tool_id, errors)
        _check_manifest_locale(manifest_path, tool_id, code_rules, label_policy, errors)
        _check_manifest_permissions(manifest, tool_id, errors)

    # Pass 2: depth-2 manifests under "Standalone tools/" (independent
    # tools) and depth-3 companion tools nested under a tool root.
    # Independent tools at depth-2 under "Standalone tools/" are treated
    # as top-level tools (they have their own owner, lifecycle, and
    # failure boundary per A184/A278).  Companion tools at depth-3
    # share their owner's permission profile.
    for manifest_path in sorted(standalone_dir.glob("*/manifest.json")):
        # These are independent tools — re-run the depth-1 checks
        # (they are top-level tools, not companions).
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid tool manifest: {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        manifest_tool_ids.add(tool_id)
        physical_owner_root = str(manifest.get("physical_owner_root") or "")
        if physical_owner_root:
            physical_owner_roots.add(physical_owner_root)
        if (
            tool_id != manifest_path.parent.name
            and physical_owner_root != manifest_path.parent.name
        ):
            errors.append(f"tool identity mismatch: {manifest_path}")
        if re.fullmatch(label_policy.tool_id_pattern, tool_id) is None:
            errors.append(f"tool identifier is not standardized: {tool_id}")
        if "name" in manifest or manifest.get("name_key") != "tool.name":
            errors.append(f"tool name label is not standardized: {tool_id}")
        _check_manifest_capabilities(manifest, tool_id, code_rules, label_policy, errors)
        _check_manifest_window(manifest, tool_id, errors)
        _check_manifest_locale(manifest_path, tool_id, code_rules, label_policy, errors)
        _check_manifest_permissions(manifest, tool_id, errors)

    # Pass 2b: depth-3 companion tools under "Standalone tools/*/"
    for manifest_path in sorted(standalone_dir.glob("*/*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid tool manifest: {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        manifest_tool_ids.add(tool_id)
        physical_owner_root = str(manifest.get("physical_owner_root") or "")
        grandparent_name = manifest_path.parent.parent.name

        # Enforce independent_tool_direct_child_only=True: a nested manifest
        # must declare a physical_owner_root that matches its grandparent
        # directory, and that grandparent must be a known physical owner root.
        if not physical_owner_root:
            errors.append(
                f"nested tool manifest lacks physical_owner_root: {manifest_path}"
            )
        elif physical_owner_root != grandparent_name:
            errors.append(
                f"nested tool physical_owner_root does not match parent root: "
                f"{manifest_path}"
            )
        elif physical_owner_root not in physical_owner_roots:
            errors.append(
                f"nested tool references unknown physical_owner_root: "
                f"{manifest_path}"
            )

        if re.fullmatch(label_policy.tool_id_pattern, tool_id) is None:
            errors.append(f"tool identifier is not standardized: {tool_id}")
        if "name" in manifest or manifest.get("name_key") != "tool.name":
            errors.append(f"tool name label is not standardized: {tool_id}")
        _check_manifest_capabilities(manifest, tool_id, code_rules, label_policy, errors)
        _check_manifest_window(manifest, tool_id, errors)
        _check_manifest_locale(manifest_path, tool_id, code_rules, label_policy, errors)
        permissions = manifest.get("permissions")
        if not isinstance(permissions, dict):
            errors.append(f"tool permissions are missing: {tool_id}")

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
    registered_tool_ids = {
        identity.bound_tool_id
        for identity in identity_group.identities
        if identity.bound_tool_id != "main-system"
        and identity.bound_tool_id not in _NON_INDEPENDENT_TOOL_IDS
    }
    manifest_independent_ids = manifest_tool_ids - _NON_INDEPENDENT_TOOL_IDS
    if registered_tool_ids != manifest_independent_ids:
        errors.append("each independent tool must have exactly one enabled identity")
    approved_independent_ids = set(code_rules.approved_tool_ids) - _NON_INDEPENDENT_TOOL_IDS
    if manifest_independent_ids != approved_independent_ids:
        errors.append("tool identifiers do not match the approved name list")
