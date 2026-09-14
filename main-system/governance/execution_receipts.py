"""A69 execution-tier receipts — universal per-request pipeline evidence.

法典依據:
- A69: EXECUTION-LAYER-TIERS: dispatch-intake > authorization-and-governance-gate
  > task-planning > specialized-executor > result-verification
  > state-event-audit-publication; VERIFY: independent-from-work-step;
  FORBID: tier-skip + executor-self-authorize + executor-self-dispatch +
  work-step-self-verify + unrecorded-result.
- A121: BOUNDARY-ENFORCEMENT: governance-gate + audit-ledger + deny-on-violation.
- A46: ledger-per-action — every action carries an audit ledger entry.

The ledger is ordered and fail-closed: skipping, repeating or self-verifying
a tier raises ``ReceiptError``; ``publish_execution_audit`` raises when the
audit entry cannot be written so callers deny instead of claiming success.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


SOVEREIGN_AUDIT_LEDGER = (
    Path(__file__).resolve().parents[2]
    / "governance_rule"
    / "runtime"
    / "sovereign_execution_audit.jsonl"
)
_AUDIT_LOCK = threading.Lock()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ReceiptError(RuntimeError):
    """Fail-closed receipt violation (A69/A121)."""


class ExecutionTier(str, Enum):
    DISPATCH_INTAKE = "dispatch-intake"
    AUTHORIZATION_GATE = "authorization-and-governance-gate"
    TASK_PLANNING = "task-planning"
    SPECIALIZED_EXECUTOR = "specialized-executor"
    RESULT_VERIFICATION = "result-verification"
    AUDIT_PUBLICATION = "state-event-audit-publication"


TIER_ORDER: tuple[ExecutionTier, ...] = tuple(ExecutionTier)


@dataclass(frozen=True)
class ExecutionReceipt:
    request_id: str
    tier: ExecutionTier
    actor: str
    outcome: str
    details: Mapping[str, Any] = field(default_factory=dict)
    recorded_at: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tier": self.tier.value,
            "actor": self.actor,
            "outcome": self.outcome,
            "details": dict(self.details),
            "recorded_at": self.recorded_at or _utc_now(),
        }


class ExecutionReceiptLedger:
    """Ordered six-tier receipt ledger (A69); violations fail closed."""

    def __init__(self, request_id: str, requester: str) -> None:
        self.request_id = str(request_id or "").strip() or "unidentified"
        self.requester = str(requester or "").strip() or "unidentified"
        self._receipts: list[ExecutionReceipt] = []

    def record(
        self,
        tier: ExecutionTier,
        actor: str,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> ExecutionReceipt:
        if not isinstance(tier, ExecutionTier):
            raise ReceiptError("invalid execution tier")
        expected = TIER_ORDER[len(self._receipts)]
        if tier is not expected:
            raise ReceiptError(
                f"tier order violation: expected {expected.value}, got {tier.value}"
            )
        actor_value = str(actor or "").strip() or "unattested"
        if (
            tier is ExecutionTier.RESULT_VERIFICATION
            and self.executor_actor
            and actor_value == self.executor_actor
        ):
            raise ReceiptError("work-step-self-verify forbidden (A69)")
        receipt = ExecutionReceipt(
            request_id=self.request_id,
            tier=tier,
            actor=actor_value,
            outcome=str(outcome or ""),
            details=dict(details or {}),
            recorded_at=_utc_now(),
        )
        self._receipts.append(receipt)
        return receipt

    @property
    def executor_actor(self) -> str:
        for receipt in self._receipts:
            if receipt.tier is ExecutionTier.SPECIALIZED_EXECUTOR:
                return receipt.actor
        return ""

    @property
    def verifier_actor(self) -> str:
        for receipt in self._receipts:
            if receipt.tier is ExecutionTier.RESULT_VERIFICATION:
                return receipt.actor
        return ""

    def complete(self) -> bool:
        return len(self._receipts) == len(TIER_ORDER)

    def missing_tiers(self) -> tuple[str, ...]:
        return tuple(tier.value for tier in TIER_ORDER[len(self._receipts) :])

    def records(self) -> list[dict[str, Any]]:
        return [receipt.to_record() for receipt in self._receipts]

    def summary(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "receipts": len(self._receipts),
            "complete": self.complete(),
            "missing_tiers": list(self.missing_tiers()),
            "executor": self.executor_actor,
            "verifier": self.verifier_actor,
            "tiers": [receipt.tier.value for receipt in self._receipts],
        }


def publish_execution_audit(
    ledger: ExecutionReceiptLedger, outcome: Mapping[str, Any]
) -> str:
    """Mandatory audit publication (A46/A121); raises when it cannot record."""
    entry = {
        "timestamp": _utc_now(),
        "operation": "sovereign-execution",
        "request_id": ledger.request_id,
        "requester": ledger.requester,
        "outcome": dict(outcome),
        "receipts": ledger.records(),
    }
    try:
        SOVEREIGN_AUDIT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        with _AUDIT_LOCK:
            with SOVEREIGN_AUDIT_LEDGER.open("a", encoding="utf-8") as handle:
                handle.write(line + os.linesep)
                handle.flush()
    except OSError as error:
        raise ReceiptError(f"audit publication failed: {error}") from error
    return f"{ledger.request_id}:{len(entry['receipts'])}"


__all__ = [
    "ExecutionReceipt",
    "ExecutionReceiptLedger",
    "ExecutionTier",
    "ReceiptError",
    "SOVEREIGN_AUDIT_LEDGER",
    "TIER_ORDER",
    "publish_execution_audit",
]
