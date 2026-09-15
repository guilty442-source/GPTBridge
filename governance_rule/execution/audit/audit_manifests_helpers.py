"""Audit manifests helpers (A185 split).

Contains the common manifest validation helper extracted from
check_tool_manifests.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _validate_top_level_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    tool_id: str,
    label_policy: Any,
    code_rules: Any,
    errors: list[str],
    manifest_tool_ids: set,
    physical_owner_roots: set,
    *,
    check_governance_lifecycle: bool = False,
) -> None:
    """Validate a top-level manifest (depth-1 or depth-2)."""
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
    if check_governance_lifecycle and tool_id == "governance_rule":
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


def _validate_nested_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    tool_id: str,
    label_policy: Any,
    code_rules: Any,
    errors: list[str],
    manifest_tool_ids: set,
    physical_owner_roots: set,
    *,
    expected_parent_name: str,
    depth_label: str,
) -> None:
    """Validate a nested companion manifest (depth-3 or depth-4)."""
    manifest_tool_ids.add(tool_id)
    physical_owner_root = str(manifest.get("physical_owner_root") or "")

    if not physical_owner_root:
        errors.append(
            f"nested tool manifest lacks physical_owner_root: {manifest_path}"
        )
    elif physical_owner_root != expected_parent_name:
        errors.append(
            f"{depth_label} physical_owner_root does not match root: "
            f"{manifest_path}"
        )
    elif physical_owner_root not in physical_owner_roots:
        errors.append(
            f"{depth_label} references unknown physical_owner_root: "
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


def _load_manifest(
    manifest_path: Path,
    errors: list[str],
) -> dict[str, Any] | None:
    """Load a manifest JSON file, appending an error on failure."""
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"invalid tool manifest: {manifest_path}: {error}")
        return None


# These are imported from the main module to avoid duplication.
# They will be available at runtime via the module that imports this helper.
def _check_manifest_capabilities(*args, **kwargs):
    from .audit_manifests import _check_manifest_capabilities as impl
    return impl(*args, **kwargs)


def _check_manifest_window(*args, **kwargs):
    from .audit_manifests import _check_manifest_window as impl
    return impl(*args, **kwargs)


def _check_manifest_locale(*args, **kwargs):
    from .audit_manifests import _check_manifest_locale as impl
    return impl(*args, **kwargs)


def _check_manifest_permissions(*args, **kwargs):
    from .audit_manifests import _check_manifest_permissions as impl
    return impl(*args, **kwargs)


__all__ = [
    "_validate_top_level_manifest",
    "_validate_nested_manifest",
    "_load_manifest",
]
