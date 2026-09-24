"""PaperExecutionEngine — fill simulation against observable market data.

Honesty contract: with only daily bars the fill is modelled at the
latest confirmed close (or next-session open) plus slippage — the engine
never claims a real intraday/book fill. Market-closed and stale quotes
reject. TW daily price limit enforced. Records market source, model,
and assumptions on every fill.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from ..market.contracts import MarketCandle
from ..backtest.rules import capability_for, rules_for
from .contracts import PaperExecution, PaperOrder


class PaperExecutionEngine:
    def __init__(self, *, slippage_bps: Decimal = Decimal("5"),
                 max_bar_age_s: float = 172800.0,
                 volume_participation: Decimal = Decimal("0.1")) -> None:
        self._slip = slippage_bps
        self._max_age = float(max_bar_age_s)
        self._vol_part = volume_participation

    def try_fill(
        self, order: PaperOrder, *,
        candle: MarketCandle | None,
        broker_id: str = "", market: str = "",
        event_seq: int = 0, now: float | None = None,
        market_open: bool | None = None,
    ) -> dict[str, Any]:
        now = now or time.time()
        assumptions = ["daily_bar_no_intraday_path"]
        if candle is None:
            return {"ok": False, "error_code": "NO_MARKET_DATA"}
        if market_open is False:
            return {"ok": True, "filled": False,
                    "reason": "market_closed"}
        age = now - candle.candle_end.timestamp()
        if age > self._max_age:
            return {"ok": False, "error_code": "STALE_MARKET_DATA",
                    "age_s": age}

        cap = capability_for(broker_id)
        if cap is not None and not cap.allows(order.order_type):
            return {"ok": False, "error_code": "ORDER_TYPE_UNSUPPORTED",
                    "capability": cap.order_types}

        rules = rules_for(market or candle.market)
        ref = Decimal(str(candle.close))
        price = ref
        if order.order_type == "limit":
            lp = order.limit_price
            if lp is None:
                return {"ok": False, "error_code": "LIMIT_PRICE_REQUIRED"}
            if order.side in ("buy", "subscribe"):
                if Decimal(str(candle.low)) > lp:
                    return {"ok": True, "filled": False,
                            "reason": "limit_not_reached"}
                price = min(Decimal(str(candle.open)), lp)
            else:
                if Decimal(str(candle.high)) < lp:
                    return {"ok": True, "filled": False,
                            "reason": "limit_not_reached"}
                price = max(Decimal(str(candle.open)), lp)
        else:
            price = Decimal(str(candle.close))
            assumptions.append("filled_at_last_confirmed_close")

        slip = price * self._slip / Decimal("10000")
        price += slip if order.side in ("buy", "subscribe") else -slip

        # TW daily limit: fill price cannot exceed ±10% band
        if rules.limit_pct is not None:
            band = Decimal(str(candle.close)) * rules.limit_pct
            hi = Decimal(str(candle.close)) + band
            lo = Decimal(str(candle.close)) - band
            price = max(lo, min(hi, price))
            assumptions.append("tw_daily_limit_band")

        qty = order.open_qty
        if candle.volume and qty > candle.volume * self._vol_part:
            qty = candle.volume * self._vol_part
            assumptions.append("volume_constraint:≤10%_bar_volume")
        if qty <= 0:
            return {"ok": True, "filled": False, "reason": "no_quantity"}

        ex = PaperExecution(
            order_id=order.order_id, account_id=order.account_id,
            instrument_id=order.instrument_id, side=order.side,
            quantity=qty, price=price, slippage=slip,
            market_source=candle.source_id, exec_model="daily_bar",
            assumptions=assumptions, event_seq=event_seq)
        return {"ok": True, "filled": True, "execution": ex,
                "partial": qty < order.open_qty}
