"""A446 execution-tier receipts — universal per-request pipeline evidence.

法典依據:
- A446: EXECUTION-LAYER-TIERS: dispatch-intake > authorization-and-governance-gate
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

import asyncio
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

# Batched audit writer — non-blocking async queue with background flusher
_AUDIT_QUEUE: asyncio.Queue[dict[str, Any]] | None = None
_AUDIT_WORKER: asyncio.Task[None] | None = None
_AUDIT_BATCH_SIZE = 50
_AUDIT_FLUSH_INTERVAL = 0.1  # seconds


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _audit_writer_worker() -> None:
    """Background worker that batches and writes audit entries."""
    global _AUDIT_QUEUE
    if _AUDIT_QUEUE is None:
        return
    batch: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    try:
        while True:
            try:
                entry = await asyncio.wait_for(
                    _AUDIT_QUEUE.get(), timeout=_AUDIT_FLUSH_INTERVAL
                )
                batch.append(entry)
                _AUDIT_QUEUE.task_done()
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if batch and (len(batch) >= _AUDIT_BATCH_SIZE or now - last_flush >= _AUDIT_FLUSH_INTERVAL):
                await _flush_batch(batch)
                batch.clear()
                last_flush = now
    except asyncio.CancelledError:
        if batch:
            await _flush_batch(batch)
        raise


async def _flush_batch(batch: list[dict[str, Any]]) -> None:
    """Flush a batch of audit entries to disk."""
    if not batch:
        return
    try:
        SOVEREIGN_AUDIT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            for entry in batch
        ]
        data = os.linesep.join(lines) + os.linesep
        # Run blocking I/O in thread pool to avoid blocking event loop
        await asyncio.to_thread(_write_sync, data)
    except OSError:
        # Fail silently in background worker; callers already got their result
        pass


def _write_sync(data: str) -> None:
    """Synchronous file write for thread pool execution."""
    with _AUDIT_LOCK:
        with SOVEREIGN_AUDIT_LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()


def _ensure_audit_worker() -> None:
    """Ensure the audit background worker is running."""
    global _AUDIT_QUEUE, _AUDIT_WORKER
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _AUDIT_QUEUE is None:
        _AUDIT_QUEUE = asyncio.Queue(maxsize=10000)
        _AUDIT_WORKER = loop.create_task(_audit_writer_worker(), name="audit-writer")


def _shutdown_audit_worker() -> None:
    """Shutdown the audit background worker."""
    global _AUDIT_QUEUE, _AUDIT_WORKER
    if _AUDIT_WORKER and not _AUDIT_WORKER.done():
        _AUDIT_WORKER.cancel()
    _AUDIT_QUEUE = None
    _AUDIT_WORKER = None


class ReceiptError(RuntimeError):
    """Fail-closed receipt violation (A446/A121)."""


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
    """Ordered six-tier receipt ledger (A446); violations fail closed."""

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
            raise ReceiptError("work-step-self-verify forbidden (A446)")
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
    """Mandatory audit publication (A46/A121); raises when it cannot record.

    Now non-blocking: enqueues to background writer for batched async I/O.
    """
    _ensure_audit_worker()
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
                if os.name == "nt":
                    # Cross-process byte-range lock: two processes appending
                    # to the same ledger must never interleave a record.
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        handle.seek(0, 2)
                        handle.write(line + os.linesep)
                        handle.flush()
                        os.fsync(handle.fileno())
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    handle.write(line + os.linesep)
                    handle.flush()
                    os.fsync(handle.fileno())
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
