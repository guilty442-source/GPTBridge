"""Protected source, readonly, and forbidden legacy checks for the governance audit."""

from __future__ import annotations

import ast
import json
import sqlite3
import stat
from pathlib import Path

from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.governance_policy import governance_policy_snapshot


FORBIDDEN_LEGACY_SOURCES = (
    "main-system/src-core/core_logger.py",
    "main-system/src-core/core_system/boundaries.py",
    "main-system/src-core/governance",
    "main-system/scripts/audit_runtime_governance.py",
    "platform_tools",
)

REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES = frozenset(
    {
        "governance_rule/execution/authentication/__init__.py",
        "governance_rule/execution/integrity/__init__.py",
        "governance_rule/execution/versioning/__init__.py",
        "governance_rule/execution/git_tiers/__init__.py",
        "governance_rule/permission_directory/execution/identity_registry/__init__.py",
        "governance_rule/permission_directory/execution/path_guard/__init__.py",
        "main-system/src-core/core_system/governance_runtime.py",
    }
)


def _is_operating_system_read_only(path: Path) -> bool:
    attributes = int(getattr(path.stat(), "st_file_attributes", 0) or 0)
    read_only_flag = int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0) or 0)
    return bool(read_only_flag and attributes & read_only_flag)


def check_protected_sources(root: Path, errors: list[str]) -> None:
    """Verify all protected governance sources exist, are valid, and read-only."""
    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()

    protected_sources = (
        *policy.authority_files,
        *directory.managed_read_only_registry_paths,
    )
    if len(protected_sources) != len(set(protected_sources)):
        errors.append("protected governance sources contain duplicates")
    if not REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES.issubset(protected_sources):
        errors.append("governance enforcement sources are not integrity protected")
    for relative in protected_sources:
        source = root / relative
        if not source.is_file():
            errors.append(f"protected governance source is missing: {relative}")
            continue
        try:
            if source.suffix == ".py":
                content = source.read_text(encoding="utf-8")
                ast.parse(content, filename=relative)
            elif source.suffix == ".ts":
                content = source.read_text(encoding="utf-8")
                if not content.strip() or "\x00" in content:
                    raise ValueError("empty or invalid TypeScript source")
            elif source.suffix == ".sqlite3":
                with sqlite3.connect(f"file:{source.as_posix()}?mode=ro&immutable=1", uri=True) as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("invalid SQLite codex")
            elif source.suffix == ".txt":
                json.loads(source.read_text(encoding="utf-8"))
            else:
                raise ValueError("unsupported protected source type")
        except (OSError, SyntaxError, UnicodeError, ValueError) as error:
            errors.append(f"protected governance source is invalid: {relative}: {error}")
        if not _is_operating_system_read_only(source):
            errors.append(f"protected governance source is not read-only: {relative}")


def check_forbidden_legacy(root: Path, errors: list[str]) -> None:
    """Verify no forbidden legacy governance sources exist."""
    for relative in FORBIDDEN_LEGACY_SOURCES:
        if (root / relative).exists():
            errors.append(f"forbidden legacy governance source exists: {relative}")
