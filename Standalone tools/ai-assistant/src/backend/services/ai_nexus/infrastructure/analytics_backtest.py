from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from .analytics_common import (
    TRADING_DAYS,
    _max_drawdown,
    _mean,
    number,
    rounded,
    utc_text,
)


class BacktestMixin:
    """Backtest and rebalance simulation methods."""

    def backtest(self, symbols: Sequence[str], strategy: str = "buy_and_hold", initial_capital: float = 1_000_000, fee_percent: float = 0.1425, slippage_percent: float = 0.05) -> dict[str, Any]:
        if strategy not in {"buy_and_hold", "equal_weight", "momentum"}:
            raise ValueError("unsupported backtest strategy")
        symbol_list = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        series = {
            symbol: {str(item["observed_at"])[:10]: number(item["close"]) for item in self.price_series(symbol, 2000)}
            for symbol in symbol_list
        }
        dates = sorted(set.intersection(*(set(values) for values in series.values()))) if series and all(series.values()) else []
        if len(dates) < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": len(dates),
                "message": "至少需要 30 個共同交易日。",
                "analysis_coverage": {
                    "requested_symbols": symbol_list,
                    "available_symbols": sorted(symbol for symbol, values in series.items() if values),
                    "common_date_count": len(dates),
                },
            }
        capital = max(1.0, initial_capital)
        target_weights = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
        cost_rate = (max(0.0, fee_percent) + max(0.0, slippage_percent)) / 100
        units = {
            symbol: (capital * target_weights[symbol])
            / (series[symbol][dates[0]] * (1 + cost_rate))
            for symbol in symbol_list
        }
        initial_transaction_cost = sum(
            units[symbol] * series[symbol][dates[0]] * cost_rate
            for symbol in symbol_list
        )
        cash = 0.0
        equity = sum(
            units[symbol] * series[symbol][dates[0]] for symbol in symbol_list
        )
        curve = [{"date": dates[0], "value": rounded(equity, 2)}]
        daily_returns: list[float] = []
        turnover_total = 0.0
        rebalance_cost = 0.0
        with self.connect() as connection:
            action_rows = connection.execute(
                """
                SELECT symbol, action_type, effective_at, ratio, cash_amount
                FROM corporate_actions
                WHERE status='approved' AND symbol IN ({})
                ORDER BY effective_at
                """.format(",".join("?" for _ in symbol_list)),
                symbol_list,
            ).fetchall()
        corporate_actions = [dict(row) for row in action_rows]
        applied_actions: list[dict[str, Any]] = []
        ignored_actions: list[dict[str, Any]] = []
        action_index = 0
        for index in range(1, len(dates)):
            previous_date, current_date = dates[index - 1], dates[index]
            while action_index < len(corporate_actions):
                action = corporate_actions[action_index]
                effective_date = str(action.get("effective_at") or "")[:10]
                if effective_date > current_date:
                    break
                action_index += 1
                if effective_date <= previous_date:
                    continue
                symbol = str(action.get("symbol") or "")
                action_type = str(action.get("action_type") or "")
                if symbol not in units:
                    continue
                if action_type == "split" and number(action.get("ratio")) > 0:
                    units[symbol] *= number(action.get("ratio"))
                    applied_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "ratio": number(action.get("ratio")),
                        }
                    )
                elif action_type in {"dividend", "fund_distribution"} and number(action.get("cash_amount")) >= 0:
                    cash_amount = units[symbol] * number(action.get("cash_amount"))
                    cash += cash_amount
                    applied_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "cash_amount": rounded(cash_amount, 4),
                        }
                    )
                else:
                    ignored_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "reason": "unsupported_in_backtest",
                        }
                    )
            previous_equity = equity
            current_values = {
                symbol: units[symbol] * series[symbol][current_date]
                for symbol in symbol_list
            }
            equity = cash + sum(current_values.values())
            if strategy in {"equal_weight", "momentum"} and index % 21 == 0:
                if strategy == "equal_weight":
                    targets = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
                else:
                    lookback = max(0, index - 60)
                    ranked = sorted(
                        symbol_list,
                        key=lambda symbol: series[symbol][previous_date] / series[symbol][dates[lookback]] - 1,
                        reverse=True,
                    )
                    selected = set(ranked[: max(1, math.ceil(len(ranked) / 2))])
                    targets = {symbol: (1 / len(selected) if symbol in selected else 0.0) for symbol in symbol_list}
                desired_before_cost = {
                    symbol: equity * targets[symbol] for symbol in symbol_list
                }
                traded_notional = sum(
                    abs(desired_before_cost[symbol] - current_values[symbol])
                    for symbol in symbol_list
                )
                transaction_cost = min(equity, traded_notional * cost_rate)
                post_cost_equity = max(0.0, equity - transaction_cost)
                turnover_total += (
                    traded_notional / (2 * equity) if equity > 0 else 0.0
                )
                rebalance_cost += transaction_cost
                units = {
                    symbol: (
                        post_cost_equity * targets[symbol]
                        / series[symbol][current_date]
                        if series[symbol][current_date] > 0
                        else 0.0
                    )
                    for symbol in symbol_list
                }
                cash = 0.0
                equity = post_cost_equity
                target_weights = targets
            daily_returns.append(
                equity / previous_equity - 1 if previous_equity > 0 else 0.0
            )
            curve.append({"date": current_date, "value": rounded(equity, 2)})
        values = [number(item["value"]) for item in curve]
        volatility = statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) if len(daily_returns) > 1 else 0.0
        years = (len(dates) - 1) / TRADING_DAYS
        annual_return = (
            (equity / capital) ** (1 / years) - 1
            if years > 0 and equity > 0 and capital > 0
            else None
        )
        sharpe = (_mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS)) if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0 else None
        ending_values = {
            symbol: units[symbol] * series[symbol][dates[-1]]
            for symbol in symbol_list
        }
        ending_total = cash + sum(ending_values.values())
        return {
            "ok": True,
            "status": "ready",
            "strategy": strategy,
            "symbols": symbol_list,
            "sample_count": len(dates),
            "initial_capital": rounded(capital, 2),
            "ending_value": rounded(equity, 2),
            "total_return_percent": rounded((equity / capital - 1) * 100, 2),
            "annualized_return_percent": rounded((annual_return or 0) * 100, 2) if annual_return is not None else None,
            "annualized_volatility_percent": rounded(volatility * 100, 2),
            "sharpe_ratio": rounded(sharpe, 3),
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2),
            "turnover_percent": rounded(turnover_total * 100, 2),
            "ending_weights": {
                symbol: (
                    rounded(ending_values[symbol] / ending_total * 100, 4)
                    if ending_total > 0
                    else None
                )
                for symbol in symbol_list
            },
            "cost_assumptions": {
                "fee_percent": fee_percent,
                "slippage_percent": slippage_percent,
                "initial_transaction_cost": rounded(initial_transaction_cost, 2),
                "rebalance_transaction_cost": rounded(rebalance_cost, 2),
                "total_transaction_cost": rounded(initial_transaction_cost + rebalance_cost, 2),
                "initial_purchase_included": True,
            },
            "methodology": "unit_based_holdings_with_natural_weight_drift",
            "lookahead_protection": "signals use only prices available before each rebalance",
            "corporate_actions": {
                "policy": "approved actions only",
                "applied": applied_actions,
                "ignored": ignored_actions,
            },
            "analysis_coverage": {
                "requested_symbols": symbol_list,
                "available_symbols": symbol_list,
                "common_date_count": len(dates),
                "start_date": dates[0],
                "end_date": dates[-1],
            },
            "assumptions": [
                "Stored close prices are treated as unadjusted prices.",
                "Approved splits adjust units and approved cash distributions enter cash.",
                "Taxes, FX conversion, delistings, survivorship bias and unapproved corporate actions are not modeled.",
            ],
            "equity_curve": curve[-800:],
        }

    def rebalance(self, state: dict[str, Any], targets: dict[str, float] | None = None, max_position_percent: float = 35, cash_reserve_percent: float = 5, min_trade_value: float = 1000, fee_percent: float = 0.1425) -> dict[str, Any]:
        positions = self.current_positions(state)
        total = sum(number(item.get("market_value")) for item in positions)
        if total <= 0:
            return {"ok": False, "status": "empty", "orders": []}
        symbols = [str(item.get("symbol") or "") for item in positions]
        raw_targets = {str(key).upper(): max(0.0, number(value)) for key, value in (targets or {}).items()}
        if not raw_targets:
            raw_targets = {symbol: 100 / len(symbols) for symbol in symbols}
        investable_percent = max(0.0, 100 - cash_reserve_percent)
        position_cap = max(0.0, min(100.0, max_position_percent))
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
        actual_invested_percent = sum(normalized.values())
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
        return {
            "ok": True,
            "status": "draft",
            "execution_policy": "simulation_only_human_approval_required",
            "portfolio_value": rounded(total, 2),
            "cash_reserve_percent": rounded(max(0.0, 100 - actual_invested_percent), 2),
            "requested_cash_reserve_percent": cash_reserve_percent,
            "max_position_percent": position_cap,
            "estimated_fees": rounded(estimated_fees, 2),
            "orders": sorted(orders, key=lambda item: number(item.get("estimated_value")), reverse=True),
            "before_risk": self.risk(state),
        }
