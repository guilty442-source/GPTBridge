"""Packager processes shutdown helpers (A185 split).

Contains the descriptor port resolution and owner candidate collection
helpers extracted from stop_verified_packaged_backend.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from packager_base import (
    DEFAULT_BACKEND_PORT,
    PACKAGE_METADATA_NAME,
)
from packager_metadata import standalone_backend_port
from packager_processes_paths import (
    legacy_standalone_backend_owner_path,
    standalone_backend_owner_path,
    standalone_project_root,
    workspace_instance_id,
)


def _valid_port(value: object) -> int | None:
    """Validate a port value."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def _resolve_descriptor_port(live_app_dir: Path) -> int | None:
    """Resolve the backend port from manifest.json or package metadata."""
    descriptor_port: int | None = None
    for descriptor_path in (
        live_app_dir / "manifest.json",
        live_app_dir / PACKAGE_METADATA_NAME,
    ):
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(descriptor, dict):
            continue
        if descriptor_path.name == "manifest.json":
            standalone = descriptor.get("standalone")
            port_value = (
                standalone.get("backend_port")
                if isinstance(standalone, dict)
                else None
            )
        else:
            port_value = descriptor.get("backend_port")
        descriptor_port = _valid_port(port_value)
        if descriptor_port is not None:
            break
    return descriptor_port


def _collect_owner_candidates(
    tool_id: str,
    expected_root_text: str,
    expected_instance: str,
    descriptor_port: int | None,
) -> list[dict[str, Any]]:
    """Collect valid owner candidates for the tool backend."""
    owner_candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[int, str]] = set()
    owner_paths = (
        standalone_backend_owner_path(tool_id),
        legacy_standalone_backend_owner_path(tool_id),
    )
    for owner_index, owner_path in enumerate(owner_paths):
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(owner, dict):
            continue
        if str(owner.get("tool_id") or "") != tool_id:
            continue
        owner_root_text = str(owner.get("project_root") or "").strip()
        if (
            not owner_root_text
            or not os.path.isabs(owner_root_text)
            or os.path.normcase(os.path.normpath(owner_root_text))
            != expected_root_text
            or str(owner.get("workspace_instance_id") or "")
            != expected_instance
        ):
            continue
        shutdown_token = str(owner.get("shutdown_token") or "")
        if re.fullmatch(r"[0-9a-fA-F]{64}", shutdown_token) is None:
            continue
        backend_port = _valid_port(owner.get("backend_port"))
        if backend_port is None:
            backend_port = (
                DEFAULT_BACKEND_PORT
                if owner_index == 1
                else descriptor_port or standalone_backend_port(tool_id)
            )
        candidate_identity = (backend_port, shutdown_token)
        if candidate_identity in seen_candidates:
            continue
        seen_candidates.add(candidate_identity)
        owner_candidates.append(
            {
                "backend_port": backend_port,
                "shutdown_token": shutdown_token,
                "owner": owner,
                "owner_path": owner_path,
            }
        )
    return owner_candidates


__all__ = [
    "_valid_port",
    "_resolve_descriptor_port",
    "_collect_owner_candidates",
]
