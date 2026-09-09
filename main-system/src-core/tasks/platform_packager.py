"""Backward-compatible re-export shim.

The original monolithic module has been subdivided into focused sub-modules.
All public names are re-exported here so that existing imports such as
``from platform_packager import iter_tools`` continue to work unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the tasks directory is on sys.path so sibling imports resolve
# when this module is imported via its absolute path.
_TASKS_DIR = Path(__file__).resolve().parent
if str(_TASKS_DIR) not in sys.path:
    sys.path.insert(0, str(_TASKS_DIR))

# -- packager_base --
from packager_base import (  # noqa: E402,F401
    DEFAULT_BACKEND_PORT,
    ELECTRON_DIST_DIR,
    MAIN_SYSTEM_ROOT,
    MAX_COMPLETED_RECOVERY_GENERATIONS,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_METADATA_NAME,
    PLATFORM_RENDERER_ROOT,
    PLATFORM_TOOLS_DIR,
    PROJECT_ROOT,
    REQUIRED_TOOL_DISPLAY_VERSION,
    REQUIRED_TOOL_VERSION,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    STANDALONE_BACKEND_PORT_COUNT,
    STANDALONE_BACKEND_PORT_MIN,
    TEMPLATE_DIR,
    TOOL_RUNTIME_CONTRACT_PATH,
    PackageOperationBusy,
    PromotionRecoveryRequired,
    _background_subprocess_kwargs,
    collect_file_hashes,
    declared_source_exclusions,
    load_package_metadata,
    load_tool_runtime_contract,
    run_upgrade_auto_repair,
    snapshot_digest,
    verify_packaged_app,
)

# -- packager_inventory --
from packager_inventory import (  # noqa: E402,F401
    _inventory_file_map,
    _inventory_package_tree,
    _is_link_or_reparse,
    _persist_package_document,
    _process_is_alive,
    _sha256_regular_file,
)

# -- packager_locking --
from packager_locking import package_operation_lock  # noqa: E402,F401

# -- packager_metadata --
from packager_metadata import (  # noqa: E402,F401
    iter_companion_renderer_tools,
    iter_tools,
    load_manifest,
    resolve_entry,
    resolve_executable_name,
    standalone_backend_port,
    tool_id_from_directory,
    validate_tool_version_baseline,
)

# -- packager_renderer --
from packager_renderer import (  # noqa: E402,F401
    build_platform_renderer,
    copy_app_templates,
    npx_command,
    renderer_output_dir,
)

# -- packager_runtime --
from packager_runtime import (  # noqa: E402,F401
    PYTHON_RUNTIME_EXCLUDED_NAMES,
    PYTHON_RUNTIME_EXCLUDED_PREFIXES,
    REQUIRED_RUNTIME_IMPORTS,
    SENSITIVE_RUNTIME_DIRECTORY_NAMES,
    SENSITIVE_RUNTIME_SUFFIXES,
    TOOL_REQUIRED_RUNTIME_IMPORTS,
    copy_electron_runtime,
    copy_file_preserving_locked_target,
    copy_portable_python_runtime,
    copy_runtime_item,
    package_copy_ignore,
    python_runtime_copy_ignore,
    validate_staged_python_runtime,
)

# -- packager_bundle --
from packager_bundle import (  # noqa: E402,F401
    SRC_CORE_DIR,
    copy_backend_source_bundle,
    copy_runtime_source,
    package_source_excluded_paths,
    package_source_roots,
)

# -- packager_processes --
from packager_processes import (  # noqa: E402,F401
    _force_stop_verified_unresponsive_backend,
    legacy_standalone_backend_owner_path,
    restart_packaged_executable,
    running_executable_process_ids,
    standalone_backend_owner_path,
    standalone_project_root,
    stop_running_executable_for_upgrade,
    stop_verified_packaged_backend,
    windows_process_owns_listening_port,
    workspace_instance_id,
)

# -- packager_distribution --
from packager_distribution import (  # noqa: E402,F401
    _promote_staged_distribution_in_place,
    _synchronize_distribution_files_in_place,
    promote_staged_distribution,
)

# -- packager_verification --
from packager_verification import (  # noqa: E402,F401
    verify_tool_package,
    verify_upgrade_result,
)

# -- packager_recovery --
from packager_recovery import prune_completed_recovery_roots  # noqa: E402,F401

# -- packager_orchestration --
from packager_orchestration import (  # noqa: E402,F401
    _package_tool_locked,
    package_tool,
)

# -- packager_main --
from packager_main import main  # noqa: E402,F401


if __name__ == "__main__":
    raise SystemExit(main())
