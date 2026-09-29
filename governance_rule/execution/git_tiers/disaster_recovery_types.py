"""Git disaster-recovery constants + records (A185 split).

Extracted from ``disaster_recovery.py`` (source-size contract): ref/state
constants, fsck classification patterns, hash helpers and the
``RecoveryPoint``/``BundleManifest``/``RepositoryDiagnosis``/
``WorktreeIntegrity``/``RecoveryPlan`` record types.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .paths import BACKUP_ROOT_RELATIVE

RECOVERY_REF_PREFIX: Final[str] = "refs/gptbridge/recovery/"
AUTOMATION_STATE_RELATIVE: Final[str] = "gptbridge-automation"
#: Backup root, relative to the repository root (A201 containment).
DEFAULT_BACKUP_ROOT: Final[str] = BACKUP_ROOT_RELATIVE.as_posix()

BUNDLE_KINDS: Final[frozenset[str]] = frozenset(
    {"release", "pre-migration", "governance-structure", "ref-maintenance", "manual"}
)

EMERGENCY_MODES: Final[tuple[str, ...]] = (
    "NORMAL",
    "DEGRADED",
    "READ_ONLY",
    "RECOVERY",
    "STOPPED",
)

READ_ONLY_ALLOWED: Final[frozenset[str]] = frozenset(
    {"status", "log", "diff", "show", "bundle verify", "fsck", "rev-parse", "cat-file", "for-each-ref"}
)

WORKTREE_STATES: Final[tuple[str, ...]] = (
    "HEALTHY",
    "MISSING_PATH",
    "BROKEN_GITFILE",
    "BROKEN_HEAD",
    "BROKEN_INDEX",
    "BRANCH_MISMATCH",
    "ORPHANED",
)

_INTEGRITY_STATES: Final[tuple[str, ...]] = ("HEALTHY", "WARN", "CORRUPT", "UNKNOWN")

_FSCK_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("CORRUPT", re.compile(r"(missing|corrupt|invalid|bad) (blob|tree|commit|tag|object)", re.I)),
    ("CORRUPT", re.compile(r"error: (object file|loose object|unable)", re.I)),
    ("CORRUPT", re.compile(r"broken (link|ref)", re.I)),
    ("WARN", re.compile(r"dangling (blob|tree|commit|tag)", re.I)),
    ("WARN", re.compile(r"unreachable (blob|tree|commit|tag)", re.I)),
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DisasterRecoveryError(RuntimeError):
    """Raised when a DR action violates its bounded contract."""


@dataclass
class RecoveryPoint:
    recovery_id: str
    timestamp: str
    main_revision: str
    origin_main_revision: str
    queue_id: str
    source_branch: str
    source_revision: str
    audit_sequence: int
    worktree_inventory_digest: str
    branch_inventory_digest: str
    git_config_digest: str
    hook_digest: str
    reason: str
    ref_name: str = ""


@dataclass
class BundleManifest:
    bundle_name: str
    bundle_sha256: str
    created_at: str
    source_repository: str
    main_revision: str
    origin_revision: str
    included_refs: list[str]
    audit_sequence: int
    git_version: str
    verified: bool
    state: str = "CREATING"
    kind: str = "manual"
    path: str = ""


@dataclass
class RepositoryDiagnosis:
    state: str
    dangling: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    broken_refs: list[str] = field(default_factory=list)
    raw_excerpt: list[str] = field(default_factory=list)


@dataclass
class WorktreeIntegrity:
    path: str
    state: str
    detail: str = ""
    branch: str = ""
    head: str = ""
    dirty: bool = False


@dataclass
class RecoveryPlanOption:
    option_id: str
    summary: str
    git_commands: list[str]
    risk_tier: int
    expected_result: str
    rollback_anchor: str
    required_approval: str


@dataclass
class RecoveryPlan:
    incident_id: str
    detected_at: str
    severity: str
    repository_state: str
    known_good_revision: str
    current_revision: str
    origin_revision: str
    recovery_points: list[str]
    bundle_available: list[str]
    affected_worktrees: list[str]
    affected_branches: list[str]
    audit_status: str
    options: list[RecoveryPlanOption] = field(default_factory=list)
    classification: str = ""


__all__ = [
    "AUTOMATION_STATE_RELATIVE", "BUNDLE_KINDS", "BundleManifest",
    "DEFAULT_BACKUP_ROOT", "DisasterRecoveryError", "EMERGENCY_MODES",
    "READ_ONLY_ALLOWED", "RECOVERY_REF_PREFIX", "RecoveryPlan",
    "RecoveryPlanOption", "RecoveryPoint", "RepositoryDiagnosis",
    "WORKTREE_STATES", "WorktreeIntegrity",
    "_FSCK_PATTERNS", "_INTEGRITY_STATES", "_sha256_file", "_sha256_text",
]
