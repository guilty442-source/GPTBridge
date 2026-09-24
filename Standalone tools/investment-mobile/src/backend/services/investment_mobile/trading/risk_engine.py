"""Risk Engine — fail-closed order evaluation.

Python façade implementing the same limit contract as the C core
(``native/risk/risk_core.c``). When the compiled core is available it takes
over evaluation; the pure-Python path is the authoritative reference
implementation and the fallback.

Fail-closed semantics: any missing limit, malformed intent, or evaluation
error results in a rejection, never a silent approval.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from .contracts import OrderIntent, Position, RiskDecision


_DEFAULT_LIMITS: dict[str, Any] = {
    "max_order_notional": 500_000.0,
    "max_position_notional": 1_000_000.0,
    "max_daily_loss": 100_000.0,
    "max_orders_per_day": 50,
    "max_single_position_weight": 0.25,
    "allowed_markets": ["tw", "us", "fund"],
    "require_price": True,
}


class RiskEngine:
    """Evaluates order intents against configured limits."""

    def __init__(self, state_dir: Path) -> None:
        self._limits_path = state_dir / "risk-limits.json"
        self._limits = dict(_DEFAULT_LIMITS)
        self._load()

    def _load(self) -> None:
        try:
            overrides = json.loads(self._limits_path.read_text(encoding="utf-8"))
            if isinstance(overrides, dict):
                self._limits.update(overrides)
        except Exception:
            pass

    @property
    def limits(self) -> dict[str, Any]:
        return dict(self._limits)

    # ------------------------------------------------------------------
    def evaluate(
        self,
        intent: OrderIntent,
        positions: Iterable[Position],
        *,
        daily_order_count: int = 0,
        daily_realized_pnl: float = 0.0,
    ) -> RiskDecision:
        reasons: list[str] = []
        checked: list[str] = []

        market = str(intent.market or "")
        checked.append("allowed_markets")
        allowed = self._limits.get("allowed_markets")
        if not isinstance(allowed, list) or market not in allowed:
            reasons.append(f"market '{market}' not in allowed_markets")

        checked.append("quantity")
        if not intent.quantity or intent.quantity <= 0:
            reasons.append("quantity must be positive")

        notional = intent.effective_notional()
        checked.append("require_price")
        if self._limits.get("require_price") and notional <= 0:
            reasons.append("no price/notional — fail closed")

        checked.append("max_order_notional")
        max_order = float(self._limits.get("max_order_notional") or 0)
        if max_order <= 0:
            reasons.append("max_order_notional limit missing")
        elif notional > max_order:
            reasons.append(f"order notional {notional:.2f} exceeds {max_order:.2f}")

        checked.append("max_orders_per_day")
        max_orders = int(self._limits.get("max_orders_per_day") or 0)
        if max_orders <= 0 or daily_order_count >= max_orders:
            reasons.append("daily order limit reached")

        checked.append("max_daily_loss")
        max_loss = float(self._limits.get("max_daily_loss") or 0)
        if max_loss <= 0 or daily_realized_pnl <= -max_loss:
            reasons.append("daily loss limit breached")

        checked.append("max_position_notional")
        position_map = {
            (p.market, p.instrument): p for p in positions
        }
        key = (market, str(intent.instrument or ""))
        existing = position_map.get(key)
        projected = (existing.notional if existing else 0.0) + (
            notional if intent.side == "buy" else -notional
        )
        max_pos = float(self._limits.get("max_position_notional") or 0)
        if max_pos <= 0:
            reasons.append("max_position_notional limit missing")
        elif projected > max_pos:
            reasons.append(
                f"projected position {projected:.2f} exceeds {max_pos:.2f}"
            )

        checked.append("max_single_position_weight")
        total_value = sum(p.market_value for p in positions) or 0.0
        weight_cap = float(self._limits.get("max_single_position_weight") or 0)
        if weight_cap > 0 and total_value > 0:
            projected_value = (
                (existing.market_value if existing else 0.0)
                + (notional if intent.side == "buy" else -notional)
            )
            if projected_value / total_value > weight_cap:
                reasons.append(
                    "single-position weight "
                    f"{projected_value / total_value:.2%} exceeds {weight_cap:.2%}"
                )

        return RiskDecision(approved=not reasons, reasons=reasons, limits_checked=checked)

    # ------------------------------------------------------------------
    def portfolio_risk_summary(
        self, positions: Iterable[Position]
    ) -> dict[str, Any]:
        positions = list(positions)
        total = sum(p.market_value for p in positions)
        concentration = max(
            (p.market_value / total for p in positions), default=0.0
        )
        return {
            "positions": len(positions),
            "total_market_value": total,
            "max_concentration": concentration,
            "limits": self.limits,
            "evaluated_at": time.time(),
        }
