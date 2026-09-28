"""Packager error handling helpers (A185 split).

Contains the error handling and return construction extracted from
_package_tool_locked.
"""
from __future__ import annotations

import shutil
import traceback
from pathlib import Path
from typing import Any

from packager_base import PromotionRecoveryRequired
from packager_processes import (
    restart_packaged_executable,
    running_executable_process_ids,
)
from packager_recovery import prune_completed_recovery_roots


def _handle_promotion_recovery_error(
    exc: PromotionRecoveryRequired,
    tool_id: str,
    entry: Path,
    exe_path: Path,
    desktop_stopped_for_upgrade: bool,
    desktop_restarted: bool,
) -> dict[str, Any]:
    """Handle PromotionRecoveryRequired exception."""
    if (
        desktop_stopped_for_upgrade
        and not running_executable_process_ids(exe_path)
    ):
        desktop_restarted = restart_packaged_executable(exe_path)
    return {
        "ok": False,
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "error_code": "PROMOTION_ROLLBACK_INCOMPLETE",
        "message": str(exc),
        "recovery_path": str(exc.recovery_root),
        "rollback_errors": list(exc.rollback_errors),
        "desktop_restarted": desktop_restarted,
    }


def _handle_generic_package_error(
    exc: Exception,
    tool_id: str,
    entry: Path,
    exe_path: Path,
    package_root: Path,
    desktop_stopped_for_upgrade: bool,
    desktop_restarted: bool,
) -> tuple[dict[str, Any], bool]:
    """Handle generic Exception during packaging.

    Returns (error_result, preserve_package_root).
    """
    retained_recovery = (
        (package_root / "promotion-aborted.json").exists()
        or (package_root / "promotion-recovery-manifest.json").exists()
        or (package_root / "previous-dist").exists()
        or any(package_root.glob("failed-new-dist*"))
    )
    preserve_package_root = bool(retained_recovery)
    if (
        desktop_stopped_for_upgrade
        and not running_executable_process_ids(exe_path)
    ):
        desktop_restarted = restart_packaged_executable(exe_path)
    error_result = {
        "ok": False,
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "error_code": (
            "PROMOTION_FAILED_RECOVERY_RETAINED"
            if retained_recovery
            else "PACKAGE_FAILED"
        ),
        "message": str(exc),
        "diagnostic": traceback.format_exc(limit=12),
        "recovery_path": (
            str(package_root) if retained_recovery else ""
        ),
        "desktop_restarted": desktop_restarted,
    }
    return error_result, preserve_package_root


def _cleanup_package_root(package_root: Path, preserve_package_root: bool) -> None:
    """Clean up the package root unless recovery is present."""
    contains_recovery = (
        (package_root / "previous-dist").exists()
        or (package_root / "previous-runtime").exists()
        or (package_root / "previous-app").exists()
        or any(package_root.glob("failed-new-dist*"))
        or (package_root / "promotion-recovery-manifest.json").exists()
        or (package_root / "promotion-aborted.json").exists()
    )
    if (
        package_root.exists()
        and not preserve_package_root
        and not contains_recovery
    ):
        shutil.rmtree(package_root, ignore_errors=True)


def _build_package_success_result(
    tool_id: str,
    entry: Path,
    exe_path: Path,
    dist_dir: Path,
    app_dir: Path,
    runtime_path: Path | None,
    python_runtime_path: Path,
    staged_verification: dict[str, Any],
    backend_port: int,
    runtime_contract: dict[str, Any],
    restart_after_upgrade: bool,
    desktop_restarted: bool,
    promotion_recovery_root: Path | None,
    tool_dir: Path,
) -> dict[str, Any]:
    """Build the success result dict."""
    pruned_recovery_paths = prune_completed_recovery_roots(tool_dir)
    return {
        "ok": exe_path.exists(),
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "renderer_path": str(dist_dir / "resources" / "app" / "renderer"),
        "runtime_path": (
            str(dist_dir / "resources" / "app" / runtime_path.relative_to(app_dir))
            if runtime_path
            else ""
        ),
        "python_runtime_path": str(
            dist_dir
            / "resources"
            / "app"
            / python_runtime_path.relative_to(app_dir)
        ),
        "package_digest": staged_verification.get("package_digest", ""),
        "backend_port": backend_port,
        "runtime_contract_version": runtime_contract["contract_version"],
        "production_upgrade": (
            "verified-stop-promote-restart"
            if restart_after_upgrade
            else "verified-promote"
        ),
        "desktop_restarted": desktop_restarted,
        "pruned_recovery_paths": pruned_recovery_paths,
        "recovery_path": (
            str(promotion_recovery_root)
            if promotion_recovery_root is not None
            else ""
        ),
    }


__all__ = [
    "_handle_promotion_recovery_error",
    "_handle_generic_package_error",
    "_cleanup_package_root",
    "_build_package_success_result",
]
