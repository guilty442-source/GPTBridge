"""ExecutionSimulationEngine — order fill simulation.

Market/limit/partial/no-fill/cancel + slippage + spread. Daily-bar mode
never pretends to know the intraday path: when a bar touches both stop
and target, the conservative order is configurable (default: stop first)
and every assumption is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass
class FillResult:
    filled: bool
    fill_kind: str                    # full|partial|none|cancelled
    quantity: Decimal
    price: Decimal
    slippage: Decimal
    fee: Decimal = Decimal("0")
    reason: str = ""
    assumptions: list[str] = None     # recorded simulation assumptions

    def __post_init__(self):
        if self.assumptions is None:
            self.assumptions = []


class ExecutionSimulationEngine:
    def __init__(
        self, *,
        slippage_bps: Decimal = Decimal("5"),
        spread_fraction: Decimal = Decimal("0"),
        same_bar_order: str = "stop_first",   # conservative default
        partial_fill_ratio: Decimal = Decimal("1"),
    ) -> None:
        self._slip_bps = slippage_bps
        self._spread = spread_fraction
        self._same_bar = same_bar_order
        self._partial = partial_fill_ratio

    # ------------------------------------------------------------------
    def simulate(
        self, *, side: str, quantity, order_type: str,
        bar: dict[str, Decimal], limit_price=None,
        volume: Decimal | None = None,
    ) -> FillResult:
        """`bar`: OHLC of the *fill* bar (never the signal bar)."""
        qty = Decimal(str(quantity))
        o, h, l, c = (Decimal(str(bar[k])) for k in ("open", "high",
                                                    "low", "close"))
        assumptions = ["daily_bar_no_intraday_path"]
        if volume is not None and qty > volume * Decimal("0.1"):
            assumptions.append("volume_constraint:≤10%_bar_volume")
            qty = min(qty, volume * Decimal("0.1"))
            partial = True
        else:
            partial = qty != Decimal(str(quantity))

        if order_type == "limit":
            lp = Decimal(str(limit_price or 0))
            if side in ("buy", "subscribe"):
                if l > lp:
                    return FillResult(False, "none", Decimal(0),
                                      Decimal(0), Decimal(0),
                                      reason="limit_not_reached",
                                      assumptions=assumptions)
                price = min(o, lp)
            else:
                if h < lp:
                    return FillResult(False, "none", Decimal(0),
                                      Decimal(0), Decimal(0),
                                      reason="limit_not_reached",
                                      assumptions=assumptions)
                price = max(o, lp)
        else:  # market → next bar open
            price = o

        slip = price * self._slip_bps / Decimal("10000")
        if side in ("buy", "subscribe"):
            price += slip
        else:
            price -= slip
        if self._spread > 0:
            price += (price * self._spread
                      if side in ("buy", "subscribe") else
                      -price * self._spread)
            assumptions.append("spread_fraction_applied")

        return FillResult(
            True, "partial" if partial else "full",
            qty, price, slip,
            reason="", assumptions=assumptions)

    def same_bar_conflict(self, *, stop_price, target_price,
                          bar: dict[str, Decimal]) -> dict[str, Any]:
        """Both stop & target touched inside one daily bar → conservative."""
        h, l = Decimal(str(bar["high"])), Decimal(str(bar["low"]))
        hit_stop = l <= Decimal(str(stop_price))
        hit_target = h >= Decimal(str(target_price))
        if hit_stop and hit_target:
            return {
                "resolution": self._same_bar,
                "filled_at": ("stop" if self._same_bar == "stop_first"
                              else "target"),
                "assumption": "same_bar_both_touched_conservative",
                "order": self._same_bar,
            }
        if hit_stop:
            return {"resolution": "stop", "filled_at": "stop"}
        if hit_target:
            return {"resolution": "target", "filled_at": "target"}
        return {"resolution": "none"}
