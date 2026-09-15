"""Rules migration, reading, and writing for file sorter CLI.

This module re-exports the names that used to live here so existing imports
(``from .cli_rules import ...``) continue to work unchanged.
"""

from __future__ import annotations

from pathlib import Path

from .cli_models import KeywordRule
from .cli_rules_document import (
    _read_custom_rules_path,
    _read_rules_document,
    _uses_explicit_legacy_rules_path,
    get_rules_path,
    read_custom_rules,
    write_custom_rules,
)
from .cli_rules_migration import (
    _legacy_rule_rejection,
    _legacy_rules_for_migration,
    _legacy_rules_migration_inbox_paths,
    _migrate_legacy_rule_values,
    _write_legacy_rules_quarantine,
)
from .cli_rules_profiles import (
    _load_profile_snapshot,
    _migrate_existing_profile_rules,
)


def _read_rules_and_revision(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> tuple[list[KeywordRule], int | None]:
    if _uses_explicit_legacy_rules_path():
        return read_custom_rules(), None
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    return (
        read_custom_rules(target_dir, state_root=state_root, profile=profile),
        snapshot.revision,
    )
