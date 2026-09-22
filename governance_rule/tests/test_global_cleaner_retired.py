"""Self-health checks for the retired global-cleaner identity."""

from __future__ import annotations

from pathlib import Path

from governance_rule.permission_directory.registries.permissions.identity_groups import (
    GLOBAL_CLEANER_IDENTITY,
)


_ROOT = Path(__file__).resolve().parents[2]


def test_global_cleaner_identity_is_retired() -> None:
    assert GLOBAL_CLEANER_IDENTITY.lifecycle == "retired"
    assert GLOBAL_CLEANER_IDENTITY.bound_tool_id == "global-cleaner"


def test_legacy_global_cleaner_sources_are_absent() -> None:
    for relative in (
        "Standalone tools/global-cleaner/src/backend/cleanup_engine.py",
        "Standalone tools/global-cleaner/src/backend/business_history.py",
        "Standalone tools/global-cleaner/src/backend/cleanup_service.py",
    ):
        assert not (_ROOT / relative).exists()


def test_global_cleaner_directory_is_removed() -> None:
    """E14: the retired tool directory was deleted after the managed
    backup archive migrated to system-rescue custody."""
    assert not (_ROOT / "Standalone tools" / "global-cleaner").exists()
