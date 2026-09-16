"""Retention Governor — audit growth and transport hot-table hygiene.

Audit growth (A46/E22 + A369):
    central audit is append-only and unbounded by nature.  Governed
    lifecycle is:

        hot period -> partition/archive -> immutable archive

    Archival must preserve the audit hash chain: the archived segment
    is sealed with its head digest, and the hot table keeps a
    continuation record linking the chain across the boundary
    (audit_chain rotation already implements this for the ledger;
    this module governs the *decision*).

Transport hot table (A369 RETENTION):
    completed / failed / dead-letter rows must not live in the hot
    claim table forever — they slow down ``FOR UPDATE SKIP LOCKED``
    claim scans.  Rows past the hot window are proposed for the
    archive table (migration 051 ``tool_request_history``).

Both sides emit proposals with evidence; execution stays governed.

Usage:
    from shared_layer.database.retention_governor import (
        AuditRetentionPolicy, evaluate_audit_growth,
        TransportRetentionPolicy, plan_transport_archive,
    )
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RetentionAction(Enum):
    NONE = "none"
    PARTITION = "partition"
    ARCHIVE = "archive"
    SEAL_ARCHIVE = "seal-archive"  # freeze segment + record digest


@dataclass(frozen=True)
class AuditRetentionPolicy:
    hot_days: int = 30
    partition_threshold_rows: int = 1_000_000
    archive_threshold_rows: int = 5_000_000
    require_chain_continuity: bool = True


@dataclass(frozen=True)
class AuditGrowthMetrics:
    hot_rows: int
    hot_age_days: float
    archive_rows: int
    chain_head_digest: Optional[str]


@dataclass(frozen=True)
class RetentionProposal:
    domain: str
    action: RetentionAction
    reason: str
    evidence: dict


def evaluate_audit_growth(
    metrics: AuditGrowthMetrics,
    policy: AuditRetentionPolicy | None = None,
) -> RetentionProposal:
    """Classify audit growth into a retention proposal.

    A chain head digest must exist before any archival proposal —
    archiving without a sealed chain boundary would break
    verifiability (A46/E22).
    """
    policy = policy or AuditRetentionPolicy()
    evidence = {
        "hot_rows": metrics.hot_rows,
        "hot_age_days": metrics.hot_age_days,
        "archive_rows": metrics.archive_rows,
        "chain_head": metrics.chain_head_digest,
    }
    if metrics.hot_rows >= policy.archive_threshold_rows:
        if policy.require_chain_continuity and not metrics.chain_head_digest:
            return RetentionProposal(
                "audit", RetentionAction.NONE,
                "archive-blocked:no-chain-head", evidence,
            )
        return RetentionProposal(
            "audit", RetentionAction.SEAL_ARCHIVE,
            "archive-threshold-exceeded", evidence,
        )
    if metrics.hot_rows >= policy.partition_threshold_rows:
        return RetentionProposal(
            "audit", RetentionAction.PARTITION,
            "partition-threshold-exceeded", evidence,
        )
    if metrics.hot_age_days > policy.hot_days:
        return RetentionProposal(
            "audit", RetentionAction.ARCHIVE,
            "hot-window-elapsed", evidence,
        )
    return RetentionProposal("audit", RetentionAction.NONE, "within", evidence)


@dataclass(frozen=True)
class TransportRetentionPolicy:
    hot_completed_seconds: float = 7 * 86_400.0
    hot_failed_seconds: float = 14 * 86_400.0
    hot_dead_letter_seconds: float = 30 * 86_400.0
    batch_limit: int = 10_000


@dataclass(frozen=True)
class TransportRow:
    request_id: str
    status: str          # completed | failed | dead-letter | queued...
    finished_at: float   # epoch seconds


@dataclass(frozen=True)
class TransportArchivePlan:
    archive_ids: tuple[str, ...]
    deferred_count: int
    reason: str


def plan_transport_archive(
    rows: list[TransportRow],
    policy: TransportRetentionPolicy | None = None,
    *,
    now: Optional[float] = None,
) -> TransportArchivePlan:
    """Classify finished transport rows for archive migration.

    Only terminal statuses past their hot window are selected;
    in-flight rows (queued/claimed/retrying) are never archived.
    Output is bounded by ``batch_limit`` — overflow defers to the
    next governed cycle.
    """
    policy = policy or TransportRetentionPolicy()
    now = now if now is not None else time.time()
    windows = {
        "completed": policy.hot_completed_seconds,
        "failed": policy.hot_failed_seconds,
        "dead-letter": policy.hot_dead_letter_seconds,
    }
    eligible = [
        row.request_id for row in rows
        if row.status in windows
        and (now - row.finished_at) >= windows[row.status]
    ]
    selected = eligible[: policy.batch_limit]
    return TransportArchivePlan(
        archive_ids=tuple(selected),
        deferred_count=len(eligible) - len(selected),
        reason="hot-window-elapsed" if selected else "within",
    )


__all__ = [
    "AuditGrowthMetrics",
    "AuditRetentionPolicy",
    "RetentionAction",
    "RetentionProposal",
    "TransportArchivePlan",
    "TransportRetentionPolicy",
    "TransportRow",
    "evaluate_audit_growth",
    "plan_transport_archive",
]
