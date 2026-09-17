"""Commit ownership, message and batch policy (task §48/§49/§50).

Ownership (§48): every AI commit is traceable to
``worker_id / task_id / actor / source_worktree`` — recorded in the
audit ledger, not crammed into the message.

Message format (§48): ``<domain>: <summary>``.  Uninformative
subjects are rejected — a self-commit that cannot say what changed
has no evidence value.

Batch control (§49): self-commit fires on a checkpoint signal, a
stability debounce, or a dirty-age threshold — bounded by
``min_commit_interval`` and ``max_changed_files`` so history is
neither per-file noise nor a two-hour mega-commit.  An in-progress
atomic edit is never cut.

Baseline (§50): ``base_revision`` is bound at allocation; every
commit records base/parent/new so stale-base workers are detectable.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .governance_manifest import timing as _manifest_timing

# Governed batch-policy defaults (manifest version source; A318-A320).
_MIN_COMMIT_INTERVAL_SECONDS: float = _manifest_timing(
    "commit_policy_min_interval_seconds", 120.0
)
_MAX_DIRTY_AGE_SECONDS: float = _manifest_timing(
    "commit_policy_max_dirty_age_seconds", 900.0
)
_MAX_CHANGED_FILES: int = int(
    _manifest_timing("commit_policy_max_changed_files", 200)
)
_COMMIT_POLICY_DEBOUNCE_SECONDS: float = _manifest_timing(
    "commit_policy_debounce_seconds", 60.0
)

#: Subjects banned as uninformative (§48).
BANNED_SUBJECTS: frozenset[str] = frozenset(
    {"update", "updates", "changes", "change", "auto", "misc",
     "fix", "wip", "tmp", "stuff"}
)

_MESSAGE_RE = re.compile(r"^([a-z0-9-]+):\s+(.+)$")
MIN_SUMMARY_WORDS = 3


class CommitTrigger(Enum):
    CHECKPOINT = "checkpoint"
    DEBOUNCE = "debounce"
    DIRTY_AGE = "dirty-age"
    NONE = "none"


@dataclass(frozen=True)
class CommitBatchPolicy:
    """When a self-commit may fire (§49)."""

    min_commit_interval_seconds: float = _MIN_COMMIT_INTERVAL_SECONDS
    max_dirty_age_seconds: float = _MAX_DIRTY_AGE_SECONDS
    max_changed_files: int = _MAX_CHANGED_FILES
    debounce_seconds: float = _COMMIT_POLICY_DEBOUNCE_SECONDS


@dataclass(frozen=True)
class CommitDecision:
    trigger: CommitTrigger
    allowed: bool
    reason: str


@dataclass(frozen=True)
class DirtyWindow:
    """Observed dirty state for batch decisions."""

    changed_files: int
    dirty_since: float      # epoch the tree first went dirty
    stable_since: float     # epoch of last modification
    last_commit_at: float
    checkpoint_signalled: bool = False


@dataclass(frozen=True)
class CommitOwnership:
    """Provenance bound to every AI commit (§48)."""

    worker_id: str
    instance_id: str
    task_id: str
    attempt_id: str
    actor: str
    source_worktree: str
    base_revision: str
    parent_revision: str
    new_revision: str


def validate_commit_message(
    domain: str, message: str
) -> tuple[bool, str]:
    """Enforce ``<domain>: <summary>`` with an informative subject."""
    match = _MESSAGE_RE.match(str(message or "").strip())
    if not match:
        return False, "format:expected '<domain>: <summary>'"
    msg_domain, summary = match.group(1), match.group(2).strip()
    if msg_domain != domain:
        return False, f"domain-mismatch:{msg_domain}!={domain}"
    first_word = summary.split()[0].lower() if summary.split() else ""
    if first_word in BANNED_SUBJECTS or summary.lower() in BANNED_SUBJECTS:
        return False, f"uninformative-subject:{summary}"
    if len(summary.split()) < MIN_SUMMARY_WORDS:
        return False, "summary-too-short"
    return True, "ok"


def decide_commit(
    window: DirtyWindow,
    policy: Optional[CommitBatchPolicy] = None,
    *,
    now: Optional[float] = None,
) -> CommitDecision:
    """Decide whether a self-commit should fire now (§49)."""
    policy = policy or CommitBatchPolicy()
    now = now if now is not None else time.time()
    if window.changed_files <= 0:
        return CommitDecision(CommitTrigger.NONE, False, "clean")
    if window.changed_files > policy.max_changed_files:
        return CommitDecision(
            CommitTrigger.NONE, False,
            f"changed-files>{policy.max_changed_files}:split-required",
        )
    since_commit = now - window.last_commit_at
    if since_commit < policy.min_commit_interval_seconds:
        return CommitDecision(
            CommitTrigger.NONE, False, "min-interval-not-met",
        )
    if window.checkpoint_signalled:
        return CommitDecision(CommitTrigger.CHECKPOINT, True, "checkpoint")
    if now - window.stable_since >= policy.debounce_seconds:
        return CommitDecision(CommitTrigger.DEBOUNCE, True, "stable")
    if now - window.dirty_since >= policy.max_dirty_age_seconds:
        return CommitDecision(
            CommitTrigger.DIRTY_AGE, True, "dirty-age-exceeded",
        )
    return CommitDecision(CommitTrigger.NONE, False, "waiting")


__all__ = [
    "BANNED_SUBJECTS",
    "CommitBatchPolicy",
    "CommitDecision",
    "CommitOwnership",
    "CommitTrigger",
    "DirtyWindow",
    "decide_commit",
    "validate_commit_message",
]
