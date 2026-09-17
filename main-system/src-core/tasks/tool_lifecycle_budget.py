"""Wall-clock budgets for independent-tool open and close.

Governor directive 2026-09-17: every independent tool must open and close
within five seconds.  The budgets are declared here and consumed by the
toolbox lifecycle mixins:

- open  : ``_launch_source_ui`` bounds its runtime-readiness wait by the
  remaining budget and reports the measured duration;
- close : ``force_close_tool`` bounds its process sweep by the remaining
  budget; the sweep itself was reduced to one native process pass.

法典依據: A294 (bounded execution latency), A446 (bounded stages),
A266 (independent-tool lifecycle isolation).
"""

from __future__ import annotations

import time
from typing import Any, Final

TOOL_OPEN_BUDGET_SECONDS: Final[float] = 5.0
TOOL_CLOSE_BUDGET_SECONDS: Final[float] = 5.0

TOOL_OPEN_BUDGET_CODE: Final[str] = "TOOL_OPEN_BUDGET_EXCEEDED"
TOOL_CLOSE_BUDGET_CODE: Final[str] = "TOOL_CLOSE_BUDGET_EXCEEDED"


def deadline_after(
    budget_seconds: float,
    *,
    started: float | None = None,
) -> float:
    reference = time.monotonic() if started is None else started
    return reference + max(0.0, float(budget_seconds))


def remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def budget_evidence(started: float, budget_seconds: float) -> dict[str, Any]:
    """Measured duration + budget for one lifecycle result (metadata only)."""
    elapsed = max(0.0, time.monotonic() - started)
    return {
        "duration_ms": int(elapsed * 1000),
        "budget_ms": int(max(0.0, float(budget_seconds)) * 1000),
        "within_budget": elapsed <= float(budget_seconds),
    }


__all__ = [
    "TOOL_CLOSE_BUDGET_CODE",
    "TOOL_CLOSE_BUDGET_SECONDS",
    "TOOL_OPEN_BUDGET_CODE",
    "TOOL_OPEN_BUDGET_SECONDS",
    "budget_evidence",
    "deadline_after",
    "remaining_seconds",
]
