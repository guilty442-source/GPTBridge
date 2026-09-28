from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packager_base import (
    PACKAGE_METADATA_NAME,
    PROJECT_ROOT,
    load_package_metadata,
    load_tool_runtime_contract,
    verify_packaged_app,
)
from packager_bundle import package_source_roots
from packager_metadata import (
    resolve_entry,
    resolve_executable_name,
    standalone_backend_port,
    validate_tool_version_baseline,
)

from core_system.versioning import component_version
from packager_verification_helpers import _collect_semantic_errors

_CENTRAL_VERSION = component_version("packager")


def verify_tool_package(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version_check = validate_tool_version_baseline(tool_id, manifest)
    if not version_check["ok"]:
        return version_check
    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"
    app_dir = dist_dir / "resources" / "app"
    if not exe_path.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_MISSING",
            "message": f"Standalone executable not found: {exe_path}",
        }
    result = verify_packaged_app(app_dir, project_root=PROJECT_ROOT)
    if not result.get("ok"):
        return {
            **result,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
        }

    metadata = load_package_metadata(app_dir)
    try:
        app_manifest = json.loads(
            (app_dir / "manifest.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        return {
            **result,
            "ok": False,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
            "error_code": "PACKAGE_SEMANTICS_INVALID",
            "message": f"Standalone app manifest is invalid: {error}",
        }
    standalone = (
        app_manifest.get("standalone")
        if isinstance(app_manifest, dict)
        else None
    )
    semantic_errors = _collect_semantic_errors(
        tool_id, tool_dir, manifest, app_dir, metadata,
        app_manifest, standalone, _CENTRAL_VERSION,
    )
    if semantic_errors:
        return {
            **result,
            "ok": False,
            "tool_id": tool_id,
            "exe_path": str(exe_path),
            "error_code": "PACKAGE_SEMANTICS_INVALID",
            "message": "; ".join(dict.fromkeys(semantic_errors)),
        }
    return {
        **result,
        "tool_id": tool_id,
        "exe_path": str(exe_path),
    }


def verify_upgrade_result(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Verify a promoted package and retry one safe rebuild if necessary."""

    if not result.get("ok"):
        return result
    verification = verify_tool_package(tool_id, tool_dir, manifest)
    if verification.get("ok"):
        return {
            **result,
            "upgrade_auto_retry": False,
            "post_verification": verification,
        }

    from packager_orchestration import package_tool

    retry = package_tool(tool_id, tool_dir, manifest)
    retry["upgrade_auto_retry"] = True
    retry["initial_post_verification"] = verification
    if not retry.get("ok"):
        return retry

    final_verification = verify_tool_package(tool_id, tool_dir, manifest)
    retry["post_verification"] = final_verification
    if final_verification.get("ok"):
        return retry
    return {
        **retry,
        "ok": False,
        "error_code": "UPGRADE_POST_VERIFY_FAILED",
        "message": str(
            final_verification.get("message")
            or "Package verification failed after automatic retry"
        ),
    }
