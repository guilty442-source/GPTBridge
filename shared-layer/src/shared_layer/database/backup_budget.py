"""Backup Budget and Retention Planner (A369 + repair-backup duty).

A backup budget is more than "backup daily".  It declares:

    keep_daily / keep_weekly / keep_monthly   — tiered retention
    dedup_enabled                             — byte-identical backups
                                                collapse to one entry
    compression                               — required for archive tier
    require_restore_tested                    — untested backups are not
                                                evidence of recoverability
    max_total_bytes                           — fleet-wide backup cap

``plan_retention`` classifies each backup ``keep`` / ``expire`` —
classification only.  Actual deletion remains a governed cleanup act
(cleanup-retention-sync-sub-sovereign; A366 confirmation applies).

Usage:
    from shared_layer.database.backup_budget import (
        BackupEntry, BackupPolicy, plan_retention,
    )
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RetentionTier(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RetentionDecision(Enum):
    KEEP = "keep"
    EXPIRE = "expire"


@dataclass(frozen=True)
class BackupEntry:
    backup_id: str
    created_at: float  # epoch seconds
    size_bytes: int
    content_hash: str
    compressed: bool = False
    restore_tested: bool = False


@dataclass(frozen=True)
class BackupPolicy:
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 12
    dedup_enabled: bool = True
    require_compression: bool = True
    require_restore_tested: bool = True
    max_total_bytes: int = 64 * 1024 ** 3


@dataclass(frozen=True)
class RetentionPlan:
    backup_id: str
    decision: RetentionDecision
    tier: Optional[RetentionTier]
    reason: str


def _tier_key(created_at: float, tier: RetentionTier) -> str:
    stamp = time.gmtime(created_at)
    if tier is RetentionTier.DAILY:
        return time.strftime("%Y-%j", stamp)
    if tier is RetentionTier.WEEKLY:
        return time.strftime("%Y-W%W", stamp)
    return time.strftime("%Y-%m", stamp)


def _pick_per_tier(
    backups: list[BackupEntry], tier: RetentionTier, keep: int
) -> set[str]:
    """Keep the newest ``keep`` periods of a tier (newest backup in
    each period wins so the restore point is maximally fresh)."""
    by_period: dict[str, BackupEntry] = {}
    for entry in sorted(backups, key=lambda b: b.created_at):
        by_period[_tier_key(entry.created_at, tier)] = entry
    periods = sorted(by_period)[-keep:] if keep else []
    return {by_period[p].backup_id for p in periods}


def plan_retention(
    backups: list[BackupEntry],
    policy: BackupPolicy | None = None,
    *,
    now: Optional[float] = None,
) -> list[RetentionPlan]:
    """Classify every backup keep/expire under the policy.

    A backup is kept if any tier claims it or it is a dedup
    representative.  Restore-untested backups are kept but flagged —
    they are evidence gaps, not silently trusted.
    """
    policy = policy or BackupPolicy()
    live = list(backups)

    # Dedup: identical content hashes collapse to the newest entry.
    deduped: list[BackupEntry] = []
    seen_hash: dict[str, BackupEntry] = {}
    if policy.dedup_enabled:
        for entry in sorted(live, key=lambda b: b.created_at):
            seen_hash[entry.content_hash] = entry
        deduped = list(seen_hash.values())
    else:
        deduped = live

    keep_ids: set[str] = set()
    for tier, count in (
        (RetentionTier.DAILY, policy.keep_daily),
        (RetentionTier.WEEKLY, policy.keep_weekly),
        (RetentionTier.MONTHLY, policy.keep_monthly),
    ):
        keep_ids |= _pick_per_tier(deduped, tier, count)

    # Capacity cap: if kept set exceeds max_total_bytes, drop oldest
    # non-representative excess first (still a proposal — never acted
    # on silently).
    kept = [b for b in deduped if b.backup_id in keep_ids]
    overflow = sum(b.size_bytes for b in kept) - policy.max_total_bytes
    if overflow > 0:
        for entry in sorted(kept, key=lambda b: b.created_at):
            if overflow <= 0:
                break
            keep_ids.discard(entry.backup_id)
            overflow -= entry.size_bytes

    plans: list[RetentionPlan] = []
    for entry in live:
        if entry not in deduped:
            plans.append(RetentionPlan(
                entry.backup_id, RetentionDecision.EXPIRE, None,
                "dedup-superseded",
            ))
        elif entry.backup_id in keep_ids:
            reason = "retained"
            if policy.require_restore_tested and not entry.restore_tested:
                reason = "retained-untested"
            if policy.require_compression and not entry.compressed:
                reason += "+uncompressed"
            plans.append(RetentionPlan(
                entry.backup_id, RetentionDecision.KEEP, None, reason
            ))
        else:
            plans.append(RetentionPlan(
                entry.backup_id, RetentionDecision.EXPIRE, None,
                "retention-window",
            ))
    return plans


__all__ = [
    "BackupEntry",
    "BackupPolicy",
    "RetentionDecision",
    "RetentionPlan",
    "RetentionTier",
    "plan_retention",
]
