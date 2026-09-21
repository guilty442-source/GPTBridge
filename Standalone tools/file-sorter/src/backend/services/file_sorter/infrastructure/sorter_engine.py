"""Sorter engine facade — lazily re-exports the decomposed domain modules.

S3: submodules are imported on first attribute access (PEP 562) instead of
eagerly at facade import, so callers that only need a few helpers don't pay
for the full engine surface.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    # sorter_types
    "DEFAULT_EXCLUDE": "sorter_types",
    "DEFAULT_INCLUDE": "sorter_types",
    "DEFAULT_JOURNAL_RETENTION_DAYS": "sorter_types",
    "DEFAULT_PLAN_TTL_SECONDS": "sorter_types",
    "DEFAULT_QUIET_SECONDS": "sorter_types",
    "FileFingerprint": "sorter_types",
    "JOURNAL_RETENTION_DAYS_ENV": "sorter_types",
    "OrganizePlan": "sorter_types",
    "PARTIAL_SUFFIXES": "sorter_types",
    "PlanOperation": "sorter_types",
    "ProfileSnapshot": "sorter_types",
    "RuleConflictError": "sorter_types",
    "SCHEMA_VERSION": "sorter_types",
    "STATE_ROOT_ENV": "sorter_types",
    "SkippedFile": "sorter_types",
    "SorterV2Error": "sorter_types",
    "StabilityResult": "sorter_types",
    "TERMINAL_TRANSACTION_STATES": "sorter_types",
    "TOOL_ROOT": "sorter_types",
    "_FileMetadata": "sorter_types",
    "_SAFE_ID_RE": "sorter_types",
    "_TARGET_LOCKS_GUARD": "sorter_types",
    "_TARGET_LOCKS_HELD": "sorter_types",
    "_utc_after": "sorter_types",
    "_utc_now": "sorter_types",
    "_validated_id": "sorter_types",
    "sha256_file": "sorter_types",
    # sorter_paths
    "_clean_patterns": "sorter_paths",
    "_clean_rule_dicts": "sorter_paths",
    "_is_link_or_reparse": "sorter_paths",
    "_plan_expired": "sorter_paths",
    "_same_path_identity": "sorter_paths",
    "_state_category_root": "sorter_paths",
    "_validate_journal_operation_paths": "sorter_paths",
    "_validate_operation_paths": "sorter_paths",
    "_validated_state_document_path": "sorter_paths",
    "_validated_target_directory": "sorter_paths",
    "profile_id_for": "sorter_paths",
    "profile_path": "sorter_paths",
    "resolve_state_root": "sorter_paths",
    # sorter_locks
    "_ExclusiveFileLock": "sorter_locks",
    "_TargetDirectoryLock": "sorter_locks",
    "_atomic_write_json": "sorter_locks",
    "_fsync_directory": "sorter_locks",
    "_lock_owner_alive": "sorter_locks",
    "_try_lock_descriptor": "sorter_locks",
    "_unlock_descriptor": "sorter_locks",
    "_windows_process_alive": "sorter_locks",
    # sorter_profiles
    "_profile_document": "sorter_profiles",
    "_read_profile_document": "sorter_profiles",
    "list_profiles": "sorter_profiles",
    "load_profile": "sorter_profiles",
    "save_profile": "sorter_profiles",
    # sorter_stability
    "_best_effort_unlocked": "sorter_stability",
    "check_file_stability": "sorter_stability",
    "is_partial_file": "sorter_stability",
    "same_volume": "sorter_stability",
    # sorter_plans
    "_document_age_days": "sorter_plans",
    "_journal_retention_days": "sorter_plans",
    "load_plan": "sorter_plans",
    "new_plan": "sorter_plans",
    "prune_state": "sorter_plans",
    "save_plan": "sorter_plans",
    # sorter_journal
    "_Journal": "sorter_journal",
    "_journal_path": "sorter_journal",
    "_read_journal": "sorter_journal",
    "transaction_history": "sorter_journal",
    # sorter_metadata
    "_capture_file_metadata": "sorter_metadata",
    "_copy_windows_exclusive": "sorter_metadata",
    "_ensure_source_unchanged": "sorter_metadata",
    "_expected_fingerprint": "sorter_metadata",
    "_file_metadata_from_dict": "sorter_metadata",
    "_file_metadata_to_dict": "sorter_metadata",
    "_fingerprint": "sorter_metadata",
    "_fsync_file": "sorter_metadata",
    "_posix_extended_attributes": "sorter_metadata",
    "_set_windows_file_attributes": "sorter_metadata",
    "_unlink_file_preserving_failure": "sorter_metadata",
    "_verify_file_metadata": "sorter_metadata",
    "_windows_alternate_streams": "sorter_metadata",
    # sorter_staging
    "_copy_file_exclusive_preserving_metadata": "sorter_staging",
    "_copy_stage_exclusive": "sorter_staging",
    "_copy_to_staging": "sorter_staging",
    "_publish_staging": "sorter_staging",
    "_same_volume_move": "sorter_staging",
    "_staged_move": "sorter_staging",
    # sorter_execute
    "_execute_plan_under_lock": "sorter_execute",
    "_existing_plan_execution": "sorter_execute",
    "_target_lock_path": "sorter_execute",
    "execute_plan": "sorter_execute",
    # sorter_undo_move
    "_move_exact_for_undo": "sorter_undo_move",
    "_resume_undo_move": "sorter_undo_move",
    "_validate_undo_move_paths": "sorter_undo_move",
    # sorter_undo
    "_journal_has_interrupted_undo": "sorter_undo",
    "_recover_journal": "sorter_undo",
    "_undo_journal": "sorter_undo",
    "_undo_transaction_under_lock": "sorter_undo",
    "recover_transactions": "sorter_undo",
    "undo_last_transaction": "sorter_undo",
    "undo_transaction": "sorter_undo",
}


def __getattr__(name: str) -> Any:
    submodule = _EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{submodule}", __package__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    "DEFAULT_EXCLUDE",
    "DEFAULT_INCLUDE",
    "DEFAULT_JOURNAL_RETENTION_DAYS",
    "DEFAULT_PLAN_TTL_SECONDS",
    "DEFAULT_QUIET_SECONDS",
    "FileFingerprint",
    "JOURNAL_RETENTION_DAYS_ENV",
    "OrganizePlan",
    "PARTIAL_SUFFIXES",
    "PlanOperation",
    "ProfileSnapshot",
    "RuleConflictError",
    "SCHEMA_VERSION",
    "STATE_ROOT_ENV",
    "SkippedFile",
    "SorterV2Error",
    "StabilityResult",
    "TERMINAL_TRANSACTION_STATES",
    "TOOL_ROOT",
    "check_file_stability",
    "execute_plan",
    "is_partial_file",
    "list_profiles",
    "load_plan",
    "load_profile",
    "new_plan",
    "profile_id_for",
    "profile_path",
    "prune_state",
    "recover_transactions",
    "resolve_state_root",
    "same_volume",
    "save_plan",
    "save_profile",
    "sha256_file",
    "transaction_history",
    "undo_last_transaction",
    "undo_transaction",
]
