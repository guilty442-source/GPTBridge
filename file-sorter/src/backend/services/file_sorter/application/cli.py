"""自動化檔案管理工具（facade for split modules）."""

from __future__ import annotations

from .cli_constants import *  # noqa: F401, F403
from .cli_models import (
    FileSorterError,
    KeywordRule,
    KeywordUpsertResult,
    LegacyRulesMigration,
    OrganizeResult,
)
from .cli_paths import (
    destination_exists_for_rule,
    destination_rule_value,
    is_absolute_destination,
    is_local_folder_name,
    list_destination_folders,
    list_source_files,
    normalize_match_text,
    normalize_text,
    resolve_destination_dir,
    resolve_target_dir,
)
from .cli_rules import (
    get_rules_path,
    read_custom_rules,
    write_custom_rules,
)
from .cli_keywords import (
    add_keywords,
    build_keyword_rules,
    keyword_matches,
    update_keyword,
    upsert_keywords,
)
from .cli_organize import (
    apply_organize_plan,
    configure_duplicate_trash_enabled,
    configure_profile_enabled,
    enabled_profile_targets,
    organize_files,
    preview_organize_files,
    run_enabled_profiles_once,
    scan_after_keyword_addition,
)
from .cli_entry import create_argument_parser, main, print_rules


__all__ = [
    "FileSorterError",
    "KeywordRule",
    "KeywordUpsertResult",
    "LegacyRulesMigration",
    "OrganizeResult",
    "add_keywords",
    "apply_organize_plan",
    "build_keyword_rules",
    "configure_duplicate_trash_enabled",
    "configure_profile_enabled",
    "create_argument_parser",
    "destination_exists_for_rule",
    "destination_rule_value",
    "enabled_profile_targets",
    "get_rules_path",
    "is_absolute_destination",
    "is_local_folder_name",
    "keyword_matches",
    "list_destination_folders",
    "list_source_files",
    "main",
    "normalize_match_text",
    "normalize_text",
    "organize_files",
    "preview_organize_files",
    "print_rules",
    "read_custom_rules",
    "resolve_destination_dir",
    "resolve_target_dir",
    "run_enabled_profiles_once",
    "scan_after_keyword_addition",
    "update_keyword",
    "upsert_keywords",
    "write_custom_rules",
]


if __name__ == "__main__":
    raise SystemExit(main())
