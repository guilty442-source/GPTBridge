"""Query cost gate.

Estimates a query's cost *before* execution and converts it into an
admission-style decision.  Governance/audit queries bypass the gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import AdaptiveEnvelope, Decision, DecisionKind, PriorityClass

_PROTECTED_WORKLOADS = frozenset({"audit", "governance_write", "critical_transport"})


@dataclass(frozen=True)
class QueryCost:
    """Pre-execution cost estimate for one query."""

    estimated_rows: int
    touches_wide_columns: bool = False
    full_scan: bool = False
    write: bool = False
    expected_rows: int | None = None

    @property
    def row_explosion(self) -> bool:
        if self.expected_rows is None or self.expected_rows <= 0:
            return False
        return self.estimated_rows >= self.expected_rows * 100


class QueryCostGate:
    """Reject, batch, background or narrow queries that are too expensive."""

    def __init__(self, envelope: AdaptiveEnvelope | None = None) -> None:
        self.envelope = envelope or AdaptiveEnvelope()

    def assess(
        self,
        cost: QueryCost,
        *,
        workload: str = "interactive",
        priority_class: PriorityClass | None = None,
        can_batch: bool = False,
        can_background: bool = False,
    ) -> Decision:
        cls = priority_class
        if workload in _PROTECTED_WORKLOADS or cls is PriorityClass.CRITICAL:
            return Decision(DecisionKind.ALLOW, f"protected:{workload}")

        if cost.estimated_rows >= self.envelope.max_rows_hard_reject:
            return Decision(
                DecisionKind.REJECT,
                "cost:hard-reject",
            )
        if cost.row_explosion and cost.full_scan:
            return Decision(DecisionKind.REJECT, "cost:row-explosion-full-scan")
        if cost.estimated_rows >= self.envelope.max_rows_background and can_background:
            return Decision(DecisionKind.DEGRADE, "cost:move-to-background")
        if cost.estimated_rows >= self.envelope.max_rows_background:
            return Decision(
                DecisionKind.REJECT,
                "cost:background-required",
            )
        if cost.estimated_rows >= self.envelope.max_rows_batch:
            if can_batch:
                return Decision(
                    DecisionKind.BATCH,
                    "cost:batch-required",
                    batch_size=self.envelope.clamp_batch(self.envelope.max_rows_batch),
                )
            return Decision(
                DecisionKind.REJECT,
                "cost:narrow-scope-required",
            )
        if cost.touches_wide_columns and cost.full_scan:
            if can_batch:
                return Decision(DecisionKind.BATCH, "cost:wide-scan-batch")
            return Decision(DecisionKind.DEFER, "cost:wide-scan", retry_after_seconds=1.0)
        return Decision(DecisionKind.ALLOW, "cost:ok")


__all__ = ["QueryCost", "QueryCostGate"]
