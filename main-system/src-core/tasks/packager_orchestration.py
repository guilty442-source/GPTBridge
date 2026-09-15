from __future__ import annotations

import json
import shutil
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from packager_base import (
    ELECTRON_DIST_DIR,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_METADATA_NAME,
    PROJECT_ROOT,
    PromotionRecoveryRequired,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    collect_file_hashes,
    snapshot_digest,
    verify_packaged_app,
    load_tool_runtime_contract,
)
from packager_bundle import (
    copy_backend_source_bundle,
    package_source_excluded_paths,
    package_source_roots,
)
from packager_locking import package_operation_lock
from packager_base import PackageOperationBusy
from packager_metadata import (
    resolve_entry,
    resolve_executable_name,
    standalone_backend_port,
    validate_tool_version_baseline,
)
from packager_processes import (
    restart_packaged_executable,
    running_executable_process_ids,
    stop_running_executable_for_upgrade,
    stop_verified_packaged_backend,
)
from packager_recovery import prune_completed_recovery_roots
from packager_renderer import build_platform_renderer, copy_app_templates
from packager_runtime import (
    copy_electron_runtime,
    copy_portable_python_runtime,
    validate_staged_python_runtime,
)
from packager_distribution import promote_staged_distribution
from packager_orchestration_staging import _stage_package
from packager_orchestration_promotion import _promote_package
from packager_orchestration_errors import (
    _handle_promotion_recovery_error,
    _handle_generic_package_error,
    _cleanup_package_root,
    _build_package_success_result,
)

from core_system.versioning import component_version

_CENTRAL_VERSION = component_version("packager")


def package_tool(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    try:
        with package_operation_lock(tool_id, tool_dir):
            return _package_tool_locked(tool_id, tool_dir, manifest)
    except PackageOperationBusy as error:
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_OPERATION_ACTIVE",
            "message": str(error),
        }
    except Exception as error:
        return {
            "ok": False,
            "tool_id": tool_id,
            "error_code": "PACKAGE_LOCK_FAILED",
            "message": str(error),
        }


def _package_tool_locked(
    tool_id: str,
    tool_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version_check = validate_tool_version_baseline(tool_id, manifest)
    if not version_check["ok"]:
        return version_check
    entry = resolve_entry(tool_dir, manifest)
    backend_port = standalone_backend_port(tool_id)
    runtime_contract = load_tool_runtime_contract()
    if not entry.exists() or entry.suffix.lower() != ".py":
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Python runtime entry not found: {entry}",
        }

    electron_exe = ELECTRON_DIST_DIR / "electron.exe"
    if not electron_exe.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Electron runtime not found: {electron_exe}",
        }

    renderer_result = build_platform_renderer(tool_id)
    if not renderer_result.get("ok"):
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "message": "Platform renderer build failed",
            "renderer_output": renderer_result.get("output", ""),
        }

    renderer_dir = Path(str(renderer_result["renderer_path"]))
    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"
    running_process_ids = running_executable_process_ids(exe_path)
    restart_after_upgrade = bool(running_process_ids)
    desktop_stopped_for_upgrade = False
    desktop_restarted = False

    package_root = tool_dir / "build" / f"package-{uuid.uuid4().hex}"
    staged_dist = package_root / "dist"
    staged_exe = staged_dist / f"{executable_name}.exe"
    preserve_package_root = False
    promotion_recovery_root: Path | None = None

    try:
        staging_result = _stage_package(
            tool_id, tool_dir, entry, manifest, renderer_dir,
            staged_dist, staged_exe, backend_port, runtime_contract, _CENTRAL_VERSION,
        )
        source_roots = staging_result["source_roots"]
        source_excluded_paths = staging_result["source_excluded_paths"]
        source_files = staging_result["source_files"]
        source_digest = staging_result["source_digest"]
        app_dir = staging_result["app_dir"]
        runtime_path = staging_result["runtime_path"]
        python_runtime_path = staging_result["python_runtime_path"]
        backend_entry_relative = staging_result["backend_entry_relative"]
        staged_verification = staging_result["staged_verification"]

        promotion_result = _promote_package(
            tool_id, exe_path, dist_dir, staged_dist,
            running_process_ids, restart_after_upgrade,
        )
        running_process_ids = promotion_result["running_process_ids"]
        restart_after_upgrade = promotion_result["restart_after_upgrade"]
        desktop_stopped_for_upgrade = promotion_result["desktop_stopped_for_upgrade"]
        desktop_restarted = promotion_result["desktop_restarted"]
        promotion_recovery_root = promotion_result["promotion_recovery_root"]
        preserve_package_root = promotion_result["preserve_package_root"]
    except PromotionRecoveryRequired as exc:
        preserve_package_root = True
        return _handle_promotion_recovery_error(
            exc, tool_id, entry, exe_path,
            desktop_stopped_for_upgrade, desktop_restarted,
        )
    except Exception as exc:
        error_result, preserve_package_root = _handle_generic_package_error(
            exc, tool_id, entry, exe_path, package_root,
            desktop_stopped_for_upgrade, desktop_restarted,
        )
        return error_result
    finally:
        _cleanup_package_root(package_root, preserve_package_root)

    return _build_package_success_result(
        tool_id, entry, exe_path, dist_dir, app_dir,
        runtime_path, python_runtime_path, staged_verification,
        backend_port, runtime_contract, restart_after_upgrade,
        desktop_restarted, promotion_recovery_root, tool_dir,
    )
