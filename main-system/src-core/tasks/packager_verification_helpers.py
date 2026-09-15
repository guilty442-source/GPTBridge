"""Packager verification helpers (A185 split).

Contains the semantic checks extracted from verify_tool_package.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packager_base import load_tool_runtime_contract
from packager_bundle import package_source_roots
from packager_metadata import (
    resolve_entry,
    standalone_backend_port,
)


def _collect_semantic_errors(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
    app_dir: Path,
    metadata: dict[str, Any],
    app_manifest: dict[str, Any] | None,
    standalone: dict[str, Any] | None,
    central_version: str,
) -> list[str]:
    """Collect semantic errors for the package verification."""
    expected_port = standalone_backend_port(tool_id)
    expected_version = str(manifest.get("version") or central_version)
    request_channel = manifest.get("request_channel")
    governed_channel = (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    )
    channel_runtime_entry = (
        str(request_channel.get("runtime_entry") or "").strip()
        if isinstance(request_channel, dict)
        else ""
    )
    expected_backend_entry = (
        f"independent_tool/{tool_id}/{channel_runtime_entry}"
        if governed_channel
        else "src-core/main.py"
    )
    runtime_contract = load_tool_runtime_contract()
    expected_protocol_version = runtime_contract["protocol_version"]
    expected_contract_version = runtime_contract["contract_version"]
    expected_python_runtime = "python/python.exe"
    semantic_errors: list[str] = []
    expected_source_roots = package_source_roots(
        tool_dir,
        resolve_entry(tool_dir, manifest),
    )
    packaged_source_roots = metadata.get("source_roots")
    normalized_packaged_roots = (
        sorted(str(item) for item in packaged_source_roots)
        if isinstance(packaged_source_roots, list)
        else []
    )
    if expected_source_roots and (
        not normalized_packaged_roots
        or not set(expected_source_roots).issubset(normalized_packaged_roots)
    ):
        semantic_errors.append(
            "package source roots do not match the current packaging contract"
        )
    if not isinstance(app_manifest, dict) or not isinstance(standalone, dict):
        semantic_errors.append("app manifest has no standalone configuration")
    else:
        _check_app_manifest_semantics(
            semantic_errors, app_manifest, standalone, tool_id,
            expected_version, expected_port, expected_backend_entry,
            governed_channel, expected_protocol_version,
            expected_python_runtime, expected_contract_version,
        )

    _check_metadata_semantics(
        semantic_errors, metadata, tool_id, manifest, expected_version,
        expected_port, expected_backend_entry, governed_channel,
        expected_protocol_version, expected_python_runtime,
        expected_contract_version,
    )

    if isinstance(standalone, dict):
        paired_fields = (
            "backend_port",
            "isolated_backend",
            "backend_entry",
            "governed_channel",
            "protocol_version",
            "runtime_contract_version",
            "python_runtime",
            "backend_service_version",
        )
        if any(
            metadata.get(field) != standalone.get(field)
            for field in paired_fields
        ):
            semantic_errors.append(
                "package metadata and app manifest standalone settings differ"
            )
    if not (app_dir / expected_backend_entry).is_file():
        semantic_errors.append("packaged backend entry is missing")
    if not (app_dir / expected_python_runtime).is_file():
        semantic_errors.append("packaged Python runtime is missing")

    return semantic_errors


def _check_app_manifest_semantics(
    errors: list[str],
    app_manifest: dict[str, Any],
    standalone: dict[str, Any],
    tool_id: str,
    expected_version: str,
    expected_port: int,
    expected_backend_entry: str,
    governed_channel: bool,
    expected_protocol_version: int,
    expected_python_runtime: str,
    expected_contract_version: int,
) -> None:
    """Check app manifest standalone configuration."""
    if str(app_manifest.get("id") or "") != tool_id:
        errors.append("app manifest tool_id does not match")
    if str(app_manifest.get("version") or "") != expected_version:
        errors.append("app manifest version does not match")
    if (
        type(standalone.get("backend_port")) is not int
        or standalone.get("backend_port") != expected_port
    ):
        errors.append("app manifest backend_port is not deterministic")
    if standalone.get("isolated_backend") is not True:
        errors.append("app manifest isolated_backend must be true")
    if standalone.get("backend_entry") != expected_backend_entry:
        errors.append("app manifest backend_entry does not match")
    if standalone.get("governed_channel") != (
        "shared-layer" if governed_channel else ""
    ):
        errors.append("app manifest governed channel does not match")
    if (
        type(standalone.get("protocol_version")) is not int
        or standalone.get("protocol_version") != expected_protocol_version
    ):
        errors.append("app manifest protocol_version does not match")
    if standalone.get("python_runtime") != expected_python_runtime:
        errors.append("app manifest python_runtime does not match")
    if (
        int(standalone.get("runtime_contract_version") or 1)
        != expected_contract_version
    ):
        errors.append("app manifest runtime contract version does not match")
    if (
        str(standalone.get("backend_service_version") or "")
        != expected_version
    ):
        errors.append("app manifest backend service version does not match")


def _check_metadata_semantics(
    errors: list[str],
    metadata: dict[str, Any],
    tool_id: str,
    manifest: dict[str, Any],
    expected_version: str,
    expected_port: int,
    expected_backend_entry: str,
    governed_channel: bool,
    expected_protocol_version: int,
    expected_python_runtime: str,
    expected_contract_version: int,
) -> None:
    """Check package metadata semantics."""
    if str(metadata.get("tool_id") or "") != tool_id:
        errors.append("package metadata tool_id does not match")
    if str(manifest.get("id") or tool_id) != tool_id:
        errors.append("source manifest tool_id does not match")
    if str(metadata.get("tool_version") or "") != expected_version:
        errors.append("package metadata tool version does not match")
    if (
        str(metadata.get("backend_service_version") or "")
        != expected_version
    ):
        errors.append("package metadata backend service version does not match")
    if (
        type(metadata.get("backend_port")) is not int
        or metadata.get("backend_port") != expected_port
    ):
        errors.append("package metadata backend_port is not deterministic")
    if metadata.get("isolated_backend") is not True:
        errors.append("package metadata isolated_backend must be true")
    if metadata.get("backend_entry") != expected_backend_entry:
        errors.append("package metadata backend_entry does not match")
    if metadata.get("governed_channel") != (
        "shared-layer" if governed_channel else ""
    ):
        errors.append("package metadata governed channel does not match")
    if (
        type(metadata.get("protocol_version")) is not int
        or metadata.get("protocol_version") != expected_protocol_version
    ):
        errors.append("package metadata protocol_version does not match")
    if metadata.get("python_runtime") != expected_python_runtime:
        errors.append("package metadata python_runtime does not match")
    if (
        int(metadata.get("runtime_contract_version") or 1)
        != expected_contract_version
    ):
        errors.append("package metadata runtime contract version does not match")


__all__ = ["_collect_semantic_errors"]
