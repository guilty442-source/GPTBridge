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

from governance_rule.execution.codex_repository import load_governance_codex

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CODEX_PATH = PROJECT_ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"


def _retired_sovereign_ids() -> set[str]:
    """Return the set of sovereign_ids whose rank indicates retirement."""
    try:
        codex = load_governance_codex()
    except Exception:
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
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(codex_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT architecture_code, target_root, current_state, "
            "required_state, legacy_root, verification_owner, "
            "old_root_deletion_state FROM architecture_activation_states"
        )
        for row in cursor.fetchall():
            arch_code, target_root, current_state, _required, legacy_root, verification_owner, old_root_deletion = row

            # 1. verification_owner must not be a retired sovereign
            if verification_owner and verification_owner in retired:
                warnings.append(
                    f"activation state verification_owner is a retired sovereign: "
                    f"{arch_code}: {verification_owner}"
                )

            # 2. target_root must exist when state is active or mandated
            if target_root and current_state in ("active", "mandated"):
                normalized = target_root.replace("\\\\", "\\")
                if not Path(normalized).exists():
                    warnings.append(
                        f"activation state target_root does not exist: "
                        f"{arch_code}: {target_root}"
                    )

            # 3. old_root_deletion_state must be consistent with filesystem
            if legacy_root and old_root_deletion == "not-applicable":
                normalized_legacy = legacy_root.replace("\\\\", "\\")
                if Path(normalized_legacy).exists():
                    warnings.append(
                        f"activation state old_root exists but deletion_state "
                        f"is not-applicable: {arch_code}: {legacy_root}"
                    )
    except sqlite3.Error as error:
        errors.append(f"activation states audit failed: {error}")
    finally:
        if conn is not None:
            conn.close()

    # Print warnings to stderr so they are visible without blocking commits.
    for warning in warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
