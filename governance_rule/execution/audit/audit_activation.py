"""Architecture activation states audit.

Checks the ``architecture_activation_states`` table for:
  1. retired sovereign identities used as ``verification_owner``
  2. ``target_root`` paths that do not exist on the filesystem
  3. ``old_root_deletion_state`` inconsistent with the actual old root

Issues are reported as warnings (printed to stderr) rather than errors,
so the audit surfaces Codex-level inconsistencies without blocking commits
when the Codex has not yet been amended.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import psycopg

from governance_rule.execution.codex_repository import (
    codex_readonly_connection,
    load_governance_codex,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CODEX_PATH = PROJECT_ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"


def _retired_sovereign_ids() -> set[str]:
    """Return the set of sovereign_ids whose rank indicates retirement."""
    try:
        codex = load_governance_codex()
    except (OSError, ValueError, KeyError, RuntimeError, ImportError,
            sqlite3.Error, psycopg.Error):
        return set()
    retired: set[str] = set()
    for sovereign in codex.sovereigns:
        rank = str(sovereign.rank or "")
        if "retired" in rank:
            retired.add(sovereign.id)
    return retired


def check_activation_states(root: Path, errors: list[str]) -> None:
    """Verify architecture_activation_states consistency.

    Reports Codex-level inconsistencies as warnings to stderr so they
    are visible without blocking the commit pipeline.  Hard failures
    (table missing, query error) are added to ``errors``.
    """
    codex_path = root / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
    if not codex_path.is_file():
        return

    retired = _retired_sovereign_ids()
    warnings: list[str] = []
    try:
        # A279 certified tooling: the audit reads the official codex
        # through the governed read-only repository interface only.
        with codex_readonly_connection(codex_path) as conn:
            cursor = conn.cursor()
            architecture_roots = {
                str(code): str(physical_root)
                for code, physical_root in cursor.execute(
                    "SELECT architecture_code, physical_root "
                    "FROM project_architecture_directory"
                )
            }
            cursor.execute(
                "SELECT architecture_code, target_root, current_state, "
                "required_state, legacy_root, verification_owner, "
                "old_root_deletion_state FROM architecture_activation_states"
            )
            rows = cursor.fetchall()
        for row in rows:
            arch_code, target_root, current_state, _required, legacy_root, verification_owner, old_root_deletion = row

            # 1. verification_owner must not be a retired sovereign
            if verification_owner and verification_owner in retired:
                warnings.append(
                    f"activation state verification_owner is a retired sovereign: "
                    f"{arch_code}: {verification_owner}"
                )

            # 2. target_root must exist when state is active or mandated
            if target_root and current_state in ("active", "mandated"):
                if target_root.startswith("ARCH_CODE:"):
                    target_code = target_root.removeprefix("ARCH_CODE:")
                    resolved_target = architecture_roots.get(target_code, "")
                    if not resolved_target:
                        warnings.append(
                            "activation state target_root code is unregistered: "
                            f"{arch_code}: {target_root}"
                        )
                        continue
                else:
                    resolved_target = target_root
                normalized = resolved_target.replace("\\\\", "\\")
                if not Path(normalized).exists():
                    warnings.append(
                        f"activation state target_root does not exist: "
                        f"{arch_code}: {target_root} -> {resolved_target}"
                    )

            # 3. old_root_deletion_state must be consistent with filesystem
            if legacy_root and old_root_deletion == "not-applicable":
                if legacy_root.startswith("ARCH_LEGACY_CODE:"):
                    continue
                normalized_legacy = legacy_root.replace("\\\\", "\\")
                if Path(normalized_legacy).exists():
                    warnings.append(
                        f"activation state old_root exists but deletion_state "
                        f"is not-applicable: {arch_code}: {legacy_root}"
                    )
    except (sqlite3.Error, psycopg.Error) as error:
        errors.append(f"activation states audit failed: {error}")

    # Print warnings to stderr so they are visible without blocking commits.
    for warning in warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
