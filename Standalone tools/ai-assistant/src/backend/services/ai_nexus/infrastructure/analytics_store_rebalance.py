from __future__ import annotations

from typing import Any

from .analytics_common import (
    number,
    rounded,
)


class AnalyticsStoreRebalanceMixin:
    """Rebalance target normalization and draft order generation."""

    def rebalance(self, state: dict[str, Any], targets: dict[str, float] | None = None, max_position_percent: float = 35, cash_reserve_percent: float = 5, min_trade_value: float = 1000, fee_percent: float = 0.1425) -> dict[str, Any]:
        positions = self.current_positions(state)
        total = sum(number(item.get("market_value")) for item in positions)
        if total <= 0:
            return {"ok": False, "status": "empty", "orders": []}
        symbols = [str(item.get("symbol") or "") for item in positions]
        raw_targets = {str(key).upper(): max(0.0, number(value)) for key, value in (targets or {}).items()}
        if not raw_targets:
            raw_targets = {symbol: 100 / len(symbols) for symbol in symbols}
        position_cap = max(0.0, min(100.0, max_position_percent))
        normalized = self._normalized_targets(
            symbols,
            raw_targets,
            investable_percent=max(0.0, 100 - cash_reserve_percent),
            position_cap=position_cap,
        )
        orders, estimated_fees = self._rebalance_orders(
            positions,
            total,
            normalized,
            min_trade_value=min_trade_value,
            fee_percent=fee_percent,
        )
        return {
            "ok": True,
            "status": "draft",
            "execution_policy": "simulation_only_human_approval_required",
            "portfolio_value": rounded(total, 2),
            "cash_reserve_percent": rounded(max(0.0, 100 - sum(normalized.values())), 2),
            "requested_cash_reserve_percent": cash_reserve_percent,
            "max_position_percent": position_cap,
            "estimated_fees": rounded(estimated_fees, 2),
            "orders": sorted(orders, key=lambda item: number(item.get("estimated_value")), reverse=True),
            "before_risk": self.risk(state),
        }

    def _normalized_targets(
        self,
        symbols: list[str],
        raw_targets: dict[str, float],
        *,
        investable_percent: float,
        position_cap: float,
    ) -> dict[str, float]:
        normalized = {symbol: 0.0 for symbol in symbols}
        active = {symbol for symbol in symbols if raw_targets.get(symbol, 0.0) > 0}
        remaining = investable_percent
        while active and remaining > 1e-9:
            active_total = sum(raw_targets[symbol] for symbol in active)
            if active_total <= 0:
                break
            capped_symbols = {
                symbol
                for symbol in active
                if remaining * raw_targets[symbol] / active_total > position_cap
            }
            if not capped_symbols:
                for symbol in active:
                    normalized[symbol] += remaining * raw_targets[symbol] / active_total
                remaining = 0.0
                break
            for symbol in capped_symbols:
                allocation = min(position_cap - normalized[symbol], remaining)
                normalized[symbol] += max(0.0, allocation)
                remaining -= max(0.0, allocation)
                active.remove(symbol)
        return normalized

    def _rebalance_orders(
        self,
        positions: list[dict[str, Any]],
        total: float,
        normalized: dict[str, float],
        *,
        min_trade_value: float,
        fee_percent: float,
    ) -> tuple[list[dict[str, Any]], float]:
        orders = []
        estimated_fees = 0.0
        for position in positions:
            symbol = str(position.get("symbol") or "")
            current_value = number(position.get("market_value"))
            target_value = total * normalized.get(symbol, 0) / 100
            difference = target_value - current_value
            if abs(difference) < max(0.0, min_trade_value):
                continue
            price = number(position.get("price"))
            quantity = abs(difference) / price if price > 0 else 0
            fee = abs(difference) * max(0.0, fee_percent) / 100
            estimated_fees += fee
            orders.append(
                {
                    "symbol": symbol,
                    "side": "BUY" if difference > 0 else "SELL",
                    "quantity": rounded(quantity, 4),
                    "estimated_value": rounded(abs(difference), 2),
                    "estimated_fee": rounded(fee, 2),
                    "current_weight_percent": position.get("weight_percent"),
                    "target_weight_percent": rounded(normalized.get(symbol, 0), 2),
                    "price": rounded(price, 4),
                }
            )
        return orders, estimated_fees


__all__ = ['AnalyticsStoreRebalanceMixin']
