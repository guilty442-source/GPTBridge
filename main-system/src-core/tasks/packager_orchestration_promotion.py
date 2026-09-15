"""Packager promotion helpers (A185 split).

Contains the _promote_package function extracted from _package_tool_locked.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from packager_base import PROJECT_ROOT, verify_packaged_app
from packager_distribution import promote_staged_distribution
from packager_processes import (
    restart_packaged_executable,
    running_executable_process_ids,
    stop_running_executable_for_upgrade,
    stop_verified_packaged_backend,
)


def _promote_package(
    tool_id: str,
    exe_path: Path,
    dist_dir: Path,
    staged_dist: Path,
    running_process_ids: set[int],
    restart_after_upgrade: bool,
) -> dict[str, Any]:
    """Promote the staged distribution and restart if needed.

    Returns a dict with keys:
      - running_process_ids (updated)
      - restart_after_upgrade (updated)
      - desktop_stopped_for_upgrade
      - desktop_restarted
      - promotion_recovery_root
      - preserve_package_root
    """
    desktop_stopped_for_upgrade = False
    desktop_restarted = False
    preserve_package_root = False
    promotion_recovery_root: Path | None = None

    latest_process_ids = running_executable_process_ids(exe_path)
    if latest_process_ids:
        running_process_ids = latest_process_ids
        restart_after_upgrade = True
    stop_verified_packaged_backend(
        tool_id,
        dist_dir / "resources" / "app",
    )
    if restart_after_upgrade:
        if not stop_running_executable_for_upgrade(
            exe_path,
            running_process_ids,
        ):
            raise RuntimeError(
                "The verified standalone tool did not stop for its "
                "production upgrade"
            )
        desktop_stopped_for_upgrade = True
    promotion_recovery_root = promote_staged_distribution(
        staged_dist,
        dist_dir,
    )
    if promotion_recovery_root is not None:
        preserve_package_root = True
        live_verification = verify_packaged_app(
            dist_dir / "resources" / "app",
            project_root=PROJECT_ROOT,
        )
        if not live_verification.get("ok"):
            raise RuntimeError(
                "Promoted package verification failed before old package "
                f"cleanup: {live_verification.get('message', 'unknown error')}"
            )
        shutil.rmtree(promotion_recovery_root)
        promotion_recovery_root = None
        preserve_package_root = False
    if restart_after_upgrade:
        desktop_restarted = restart_packaged_executable(exe_path)
        if not desktop_restarted:
            raise RuntimeError(
                "The package was promoted but the upgraded tool could "
                "not be restarted automatically"
            )

    return {
        "running_process_ids": running_process_ids,
        "restart_after_upgrade": restart_after_upgrade,
        "desktop_stopped_for_upgrade": desktop_stopped_for_upgrade,
        "desktop_restarted": desktop_restarted,
        "promotion_recovery_root": promotion_recovery_root,
        "preserve_package_root": preserve_package_root,
    }


__all__ = ["_promote_package"]
