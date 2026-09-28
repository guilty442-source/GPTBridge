"""Packager processes — facade re-exporting the split submodules.

Original module split per A430/A68 into:
- packager_processes_probe.py (process probes, restart, stop-for-upgrade)
- packager_processes_paths.py (standalone paths, workspace instance id)
- packager_processes_shutdown.py (verified packaged backend shutdown)
- packager_processes.py (this facade)
"""

from __future__ import annotations

# Re-export the public API for existing callers.
from packager_processes_probe import (
    restart_packaged_executable,
    running_executable_process_ids,
    stop_running_executable_for_upgrade,
    windows_process_owns_listening_port,
)
from packager_processes_paths import (
    legacy_standalone_backend_owner_path,
    standalone_backend_owner_path,
    standalone_project_root,
    workspace_instance_id,
)
from packager_processes_shutdown import (
    _force_stop_verified_unresponsive_backend,
    stop_verified_packaged_backend,
)


__all__ = [
    "_force_stop_verified_unresponsive_backend",
    "legacy_standalone_backend_owner_path",
    "restart_packaged_executable",
    "running_executable_process_ids",
    "standalone_backend_owner_path",
    "standalone_project_root",
    "stop_running_executable_for_upgrade",
    "stop_verified_packaged_backend",
    "windows_process_owns_listening_port",
    "workspace_instance_id",
]
