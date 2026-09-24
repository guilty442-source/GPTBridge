"""OrderLifecycle — the only legal way a LiveOrder changes state.

Front-ends never mutate state; every transition is validated against
``_TRANSITIONS`` and journaled as an order event. ``SUBMISSION_UNKNOWN``
has no path that resubmits — only broker-reconciliation outcomes move it.
"""

from __future__ import annotations

import time
from typing import Any

from .contracts import LiveOrder, LiveOrderState, is_terminal, legal_transition


class OrderLifecycle:
    def __init__(self, record_event) -> None:
        """``record_event(order, to_state, reason)`` — journal callback."""
        self._record = record_event

    def transition(self, order: LiveOrder, target: LiveOrderState,
                   reason: str = "") -> dict[str, Any]:
        try:
            src = LiveOrderState(order.state)
        except ValueError:
            return {"ok": False, "error_code": "STATE_UNKNOWN",
                    "state": order.state}
        if is_terminal(src):
            return {"ok": False, "error_code": "ORDER_TERMINAL",
                    "state": src.value}
        if not legal_transition(src, target):
            return {"ok": False, "error_code": "ILLEGAL_TRANSITION",
                    "from": src.value, "to": target.value}
        order.state = target.value
        order.updated_at = time.time()
        self._record(order, target.value, reason)
        return {"ok": True, "order_id": order.internal_order_id,
                "state": target.value}
