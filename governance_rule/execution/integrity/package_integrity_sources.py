"""Package integrity source verification helpers (A185 split).

Contains the source verification logic extracted from verify_packaged_app.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .package_integrity import (
    SOURCE_IGNORED_DIRECTORY_NAMES,
    _normalized_metadata_paths,
    _path_is_at_or_below_root,
    _path_is_below_root,
    collect_file_hashes,
    declared_source_exclusions,
    snapshot_digest,
)


def _verify_package_sources(
    metadata: dict[str, Any],
    project_root: Path,
) -> dict[str, Any] | None:
    """Verify the package source snapshot against the current source tree.

    Returns None if source verification is unavailable, or a dict with:
      - source_check: the verification status string
    On failure, returns an error dict with ok=False.
    """
    source_check = "unavailable"
    raw_source_roots = metadata.get("source_roots")
    expected_source = metadata.get("source_files")
    if raw_source_roots is None and expected_source is None:
        return {"source_check": source_check}

    source_roots = _normalized_metadata_paths(
        raw_source_roots,
        allow_dot=True,
    )
    source_excluded_paths = _normalized_metadata_paths(
        metadata.get("source_excluded_paths", []),
    )
    expected_source_valid = (
        isinstance(expected_source, dict)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in expected_source.items()
        )
    )
    expected_source_paths = (
        _normalized_metadata_paths(list(expected_source))
        if expected_source_valid
        else None
    )
    tool_id = str(metadata.get("tool_id", ""))
    declared_exclusions = declared_source_exclusions(
        project_root,
        tool_id,
    )
    exclusion_scope_valid = (
        source_excluded_paths is not None
        and (
            (
                declared_exclusions is None
                and not source_excluded_paths
            )
            or frozenset(source_excluded_paths)
            == declared_exclusions
        )
    )
    if (
        source_roots is None
        or not source_roots
        or source_excluded_paths is None
        or expected_source_paths is None
        or not exclusion_scope_valid
        or any(
            not any(
                _path_is_below_root(excluded_path, source_root)
                for source_root in source_roots
            )
            for excluded_path in source_excluded_paths
        )
        or any(
            not any(
                _path_is_at_or_below_root(
                    expected_path,
                    source_root,
                )
                for source_root in source_roots
            )
            for expected_path in expected_source_paths
        )
        or any(
            excluded_path in expected_source
            for excluded_path in source_excluded_paths
        )
    ):
        return {
            "ok": False,
            "error_code": "PACKAGE_UNVERIFIED",
            "message": "Package metadata has an invalid source snapshot",
        }
    tool_source_root = tool_id
    effective_source_roots = [
        item
        for item in source_roots
        if _path_is_at_or_below_root(item, tool_source_root)
        or item == "main-system/config/tool-runtime-contract.json"
        or _path_is_at_or_below_root(
            item,
            "shared-layer/src/shared_layer",
        )
    ]
    legacy_infrastructure_snapshot = (
        tuple(effective_source_roots) != tuple(source_roots)
    )
    effective_expected_source = (
        {
            path: digest
            for path, digest in expected_source.items()
            if any(
                _path_is_at_or_below_root(path, source_root)
                for source_root in effective_source_roots
            )
        }
        if legacy_infrastructure_snapshot
        else expected_source
    )
    roots_available = bool(effective_source_roots) and all(
        (project_root / item).exists()
        for item in effective_source_roots
    )
    if not roots_available:
        return {"source_check": source_check}

    try:
        current_source = collect_file_hashes(
            project_root,
            effective_source_roots,
            ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
            excluded_relative_paths=frozenset(
                source_excluded_paths
            ),
        )
    except (OSError, ValueError) as error:
        return {
            "ok": False,
            "error_code": "STALE_PACKAGE",
            "message": f"Could not verify package inputs: {error}",
        }
    current_source_digest = snapshot_digest(current_source)
    expected_source_digest = (
        snapshot_digest(effective_expected_source)
        if legacy_infrastructure_snapshot
        else str(metadata.get("source_digest", ""))
    )
    if (
        current_source != effective_expected_source
        or current_source_digest != expected_source_digest
    ):
        return {
            "ok": False,
            "error_code": "STALE_PACKAGE",
            "message": "Source files changed after this EXE was packaged",
            "expected_digest": expected_source_digest,
            "actual_digest": current_source_digest,
        }
    source_check = (
        "current-runtime-contract"
        if legacy_infrastructure_snapshot
        else "current"
    )
    return {"source_check": source_check}


__all__ = ["_verify_package_sources"]
