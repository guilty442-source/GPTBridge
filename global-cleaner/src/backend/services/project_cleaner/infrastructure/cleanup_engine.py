from __future__ import annotations

import json
import re
from pathlib import Path

from .cleanup_helpers import *  # noqa: F401, F403
from .cleanup_helpers import SECONDS_PER_DAY, LEGACY_QUARANTINE_ROOT_NAME, LEGACY_RECOVERY_ROOT_NAME, QUARANTINE_RELATIVE_PATH, RECOVERY_RELATIVE_PATH, CLEANER_RUNTIME_RELATIVE_PATH, DEFAULT_QUARANTINE_TTL_HOURS, DEFAULT_PLAN_TTL_MINUTES, MAX_REPORTED_SKIPS, MAX_HISTORY_RECORDS, PROGRESS_JSON_PREFIX, MANAGED_BACKUP_SCHEMA_VERSION, MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_RELATIVE_ROOT, BACKUP_EXTRACT_RELATIVE_ROOT, SYSTEM_RESCUE_REQUIRED_PATHS, SYSTEM_RESCUE_REPAIR_ANOMALIES, PLAN_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION, CORE_EXCLUDED_DIRECTORY_NAMES, CORE_PROTECTED_RELATIVE_PATHS, SOURCE_LIKE_SUFFIXES, _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS, FALLBACK_RULES, ProgressCallback, _background_subprocess_kwargs, _deep_merge, _parse_iso
from .cleanup_scanner import CleanupScannerMixin
from .cleanup_planner import CleanupPlannerMixin
from .cleanup_executor import CleanupExecutorMixin


def _project_cleanup_version() -> str:
    """Resolve the project cleaner version from the governed manifest.

    The preferred source is the shared component-version registry
    (``core_system.versioning.component_version``).  When the main-system
    ``src-core`` tree is not importable — for example when running the tool
    standalone — the tool's own ``manifest.json`` is used instead.
    """

    try:
        from core_system.versioning import component_version

        return component_version("global-cleaner")
    except Exception:
        pass
    manifest_path = (
        Path(__file__).resolve().parents[5] / "manifest.json"
    )
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            return version
    except Exception:
        pass
    return "1.0.0"


class ProjectCleanupService(CleanupScannerMixin, CleanupPlannerMixin, CleanupExecutorMixin):
    VERSION = _project_cleanup_version()


__all__ = ["ProjectCleanupService", "PROGRESS_JSON_PREFIX"]
