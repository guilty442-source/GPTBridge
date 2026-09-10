"""Third-party startup deadline check and signal — A199/E173.

Signal production and deadline verification extracted from
third_party_governance for source-size compliance (A185/E160).
Signals are read-only; they never mutate state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core_system.third_party_types import (
    STARTUP_HARD_DEADLINE_MS,
    STARTUP_REQUIRED_FLOWS,
)


@dataclass(frozen=True)
class StartupDeadlineCheck:
    """Result of verifying the 10-second startup deadline (A199).

    Per A199: ``HARD-DEADLINE:10000ms inclusive`` and ``AT-10000MS:not-fully-
    ready=>atomically-mark-generation-failed+cancel-unfinished-owned-startup-
    work+reverse-DAG-cleanup+preserve-running-independent-tools+keep-UI-visible``.
    """

    ok: bool
    elapsed_ms: int
    fully_ready: bool
    completed_flows: tuple[str, ...]
    missing_flows: tuple[str, ...]
    phase_timings: dict[str, int]
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_startup_deadline(
    *,
    elapsed_ms: int,
    fully_ready: bool,
    completed_flows: tuple[str, ...],
    phase_timings: dict[str, int] | None = None,
) -> StartupDeadlineCheck:
    """Verify the 10-second startup deadline (A199/E173).

    Per A199: ``HARD-DEADLINE:10000ms inclusive`` and ``ACCEPTANCE:p50/p95/p99
    and-every-observed-startup individually<=10000ms+no-average-masking``.
    """
    violations: list[str] = []
    timings = phase_timings or {}

    # Check hard deadline
    if elapsed_ms > STARTUP_HARD_DEADLINE_MS:
        violations.append(f"exceeds-hard-deadline:{elapsed_ms}>{STARTUP_HARD_DEADLINE_MS}")

    # Check fully-ready requires all flows complete
    missing = tuple(
        flow for flow in STARTUP_REQUIRED_FLOWS
        if flow not in completed_flows
    )
    if fully_ready and missing:
        violations.append(f"fully-ready-with-missing-flows:{missing}")

    if not fully_ready and elapsed_ms <= STARTUP_HARD_DEADLINE_MS:
        # Not ready but within deadline — not a violation, just not ready
        pass

    # Check phase budgets (informational; carry allowed)
    total_budget = sum(timings.values())
    if total_budget > STARTUP_HARD_DEADLINE_MS:
        violations.append(
            f"phase-total-exceeds-deadline:{total_budget}>{STARTUP_HARD_DEADLINE_MS}"
        )

    return StartupDeadlineCheck(
        ok=len(violations) == 0,
        elapsed_ms=elapsed_ms,
        fully_ready=fully_ready,
        completed_flows=completed_flows,
        missing_flows=missing,
        phase_timings=timings,
        violations=tuple(violations),
    )


def startup_deadline_signal(
    *,
    elapsed_ms: int,
    failed_node: str = "",
    generation_id: str = "",
) -> dict[str, Any]:
    """Produce a signal for startup deadline failure (A199: AT-10000MS).

    Per A199: ``AT-10000MS:not-fully-ready=>atomically-mark-generation-failed+
    cancel-unfinished-owned-startup-work+reverse-DAG-cleanup+preserve-running-
    independent-tools+keep-UI-visible+publish phase/node/elapsed/remaining/
    timeout/evidence/remediation``.
    """
    return {
        "signal_type": "startup-deadline-exceeded",
        "authority": "signal-only",
        "basis": "A199/E173",
        "elapsed_ms": elapsed_ms,
        "deadline_ms": STARTUP_HARD_DEADLINE_MS,
        "failed_node": failed_node,
        "generation_id": generation_id,
        "action": (
            "mark-generation-failed+cancel-unfinished-owned-startup-work+"
            "reverse-DAG-cleanup+preserve-running-independent-tools+"
            "keep-UI-visible+publish-exact-bottleneck"
        ),
        "retry": "automatic-new-generation-after-bounded-backoff",
        "deadline_reset_within_generation": False,
        "manual_action_required": False,
        "false_ready": False,
    }
