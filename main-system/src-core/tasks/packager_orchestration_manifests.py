"""Packager manifest and metadata helpers (A185 split).

Contains the manifest/metadata writing extracted from _stage_package.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packager_base import (
    PACKAGE_FORMAT_VERSION,
    PACKAGE_METADATA_NAME,
    PROJECT_ROOT,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    collect_file_hashes,
    snapshot_digest,
)


def _resolve_backend_entry(
    tool_id: str,
    manifest: dict[str, Any],
) -> str:
    """Resolve the backend entry relative path and validate governed channel."""
    request_channel = manifest.get("request_channel")
    governed_channel = (
        isinstance(request_channel, dict)
        and request_channel.get("model")
        == "governance-authenticated-shared-layer"
    )
    channel_runtime_entry = (
        str(request_channel.get("runtime_entry") or "").strip()
        if isinstance(request_channel, dict)
        else ""
    )
    backend_entry_relative = (
        f"independent_tool/{tool_id}/{channel_runtime_entry}"
        if governed_channel
        else "src-core/main.py"
    )
    if governed_channel and (
        not channel_runtime_entry
        or Path(channel_runtime_entry).is_absolute()
        or ".." in Path(channel_runtime_entry).parts
    ):
        raise RuntimeError("Invalid governed request channel runtime entry")
    return backend_entry_relative


def _write_app_manifests(
    app_dir: Path,
    tool_id: str,
    manifest: dict[str, Any],
    backend_entry_relative: str,
    backend_port: int,
    runtime_contract: dict[str, Any],
    central_version: str,
) -> None:
    """Write the app manifest.json and package.json."""
    app_manifest = dict(manifest)
    app_manifest["id"] = tool_id
    app_manifest["version"] = str(manifest.get("version") or central_version)
    app_manifest["standalone"] = {
        "backend_entry": backend_entry_relative,
        "backend_service_version": str(manifest.get("version") or central_version),
        "backend_port": backend_port,
        "isolated_backend": True,
        "protocol_version": runtime_contract["protocol_version"],
        "runtime_contract_version": runtime_contract["contract_version"],
        "python_runtime": "python/python.exe",
        "governed_channel": "shared-layer" if _is_governed_channel(manifest) else "",
    }
    (app_dir / "manifest.json").write_text(
        json.dumps(app_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (app_dir / "package.json").write_text(
        json.dumps(
            {
                "name": f"gptbridge-tool-{tool_id}",
                "version": str(manifest.get("version") or central_version),
                "main": "main.cjs",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_package_metadata(
    app_dir: Path,
    tool_id: str,
    manifest: dict[str, Any],
    backend_port: int,
    runtime_contract: dict[str, Any],
    central_version: str,
    backend_entry_relative: str,
    source_roots: list[str],
    source_excluded_paths: list[str],
    source_files: dict[str, Any],
    source_digest: str,
) -> dict[str, Any]:
    """Write the package metadata file and return it."""
    payload_files = collect_file_hashes(
        app_dir,
        ["."],
        excluded_relative_paths=frozenset({PACKAGE_METADATA_NAME}),
    )
    package_metadata = {
        "format_version": PACKAGE_FORMAT_VERSION,
        "tool_id": tool_id,
        "tool_version": str(manifest.get("version") or central_version),
        "backend_service_version": str(manifest.get("version") or central_version),
        "backend_port": backend_port,
        "isolated_backend": True,
        "protocol_version": runtime_contract["protocol_version"],
        "runtime_contract_version": runtime_contract["contract_version"],
        "backend_entry": backend_entry_relative,
        "python_runtime": "python/python.exe",
        "governed_channel": "shared-layer" if _is_governed_channel(manifest) else "",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_roots": source_roots,
        "source_excluded_paths": source_excluded_paths,
        "source_files": source_files,
        "source_digest": source_digest,
        "payload_files": payload_files,
        "payload_digest": snapshot_digest(payload_files),
    }
    (app_dir / PACKAGE_METADATA_NAME).write_text(
        json.dumps(package_metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return package_metadata


def _is_governed_channel(manifest: dict[str, Any]) -> bool:
    """Check if the manifest uses a governed channel."""
    request_channel = manifest.get("request_channel")
    return (
        isinstance(request_channel, dict)
        and request_channel.get("model")
        == "governance-authenticated-shared-layer"
    )


__all__ = [
    "_resolve_backend_entry",
    "_write_app_manifests",
    "_write_package_metadata",
    "_is_governed_channel",
]
