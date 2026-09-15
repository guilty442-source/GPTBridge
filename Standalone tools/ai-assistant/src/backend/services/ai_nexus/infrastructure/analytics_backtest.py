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


_BACKTEST_ASSUMPTIONS = [
    "Stored close prices are treated as unadjusted prices.",
    "Approved splits adjust units and approved cash distributions enter cash.",
    "Taxes, FX conversion, delistings, survivorship bias and unapproved corporate actions are not modeled.",
]


class BacktestMixin:
    """Backtest and rebalance simulation methods."""

    def backtest(self, symbols: Sequence[str], strategy: str = "buy_and_hold", initial_capital: float = 1_000_000, fee_percent: float = 0.1425, slippage_percent: float = 0.05) -> dict[str, Any]:
        if strategy not in {"buy_and_hold", "equal_weight", "momentum"}:
            raise ValueError("unsupported backtest strategy")
        symbol_list = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        series = self._backtest_series(symbol_list)
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
        sim = self._backtest_simulation(
            series, dates, symbol_list, strategy,
            initial_capital, fee_percent, slippage_percent,
        )
        return self._backtest_result(series, dates, symbol_list, strategy, sim)

    def _backtest_series(
        self, symbol_list: list[str]
    ) -> dict[str, dict[str, float]]:
        return {
            symbol: {str(item["observed_at"])[:10]: number(item["close"]) for item in self.price_series(symbol, 2000)}
            for symbol in symbol_list
        }

    def _backtest_corporate_actions(
        self, symbol_list: list[str]
    ) -> list[dict[str, Any]]:
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
        return [dict(row) for row in action_rows]

    def _backtest_simulation(
        self,
        series: dict[str, dict[str, float]],
        dates: list[str],
        symbol_list: list[str],
        strategy: str,
        initial_capital: float,
        fee_percent: float,
        slippage_percent: float,
    ) -> dict[str, Any]:
        capital = max(1.0, initial_capital)
        cost_rate = (max(0.0, fee_percent) + max(0.0, slippage_percent)) / 100
        units = {
            symbol: (capital * (1 / len(symbol_list)))
            / (series[symbol][dates[0]] * (1 + cost_rate))
            for symbol in symbol_list
        }
        sim: dict[str, Any] = {
            "capital": capital,
            "cost_rate": cost_rate,
            "fee_percent": fee_percent,
            "slippage_percent": slippage_percent,
            "units": units,
            "cash": 0.0,
            "equity": sum(
                units[symbol] * series[symbol][dates[0]] for symbol in symbol_list
            ),
            "turnover_total": 0.0,
            "rebalance_cost": 0.0,
            "initial_transaction_cost": sum(
                units[symbol] * series[symbol][dates[0]] * cost_rate
                for symbol in symbol_list
            ),
            "applied_actions": [],
            "ignored_actions": [],
            "corporate_actions": self._backtest_corporate_actions(symbol_list),
        }
        sim["curve"], sim["daily_returns"] = self._run_backtest_loop(
            series, dates, symbol_list, strategy, sim
        )
        sim["volatility"], sim["annual_return"], sim["sharpe"] = (
            self._backtest_metrics(sim, dates)
        )
        return sim

    def _run_backtest_loop(
        self,
        series: dict[str, dict[str, float]],
        dates: list[str],
        symbol_list: list[str],
        strategy: str,
        sim: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[float]]:
        curve = [{"date": dates[0], "value": rounded(sim["equity"], 2)}]
        daily_returns: list[float] = []
        action_index = 0
        for index in range(1, len(dates)):
            previous_date, current_date = dates[index - 1], dates[index]
            action_index = self._apply_backtest_actions(
                sim, action_index, previous_date, current_date
            )
            previous_equity = sim["equity"]
            current_values = {
                symbol: sim["units"][symbol] * series[symbol][current_date]
                for symbol in symbol_list
            }
            sim["equity"] = sim["cash"] + sum(current_values.values())
            if strategy in {"equal_weight", "momentum"} and index % 21 == 0:
                self._backtest_rebalance_step(
                    sim, series, dates, symbol_list, strategy,
                    index, previous_date, current_date, current_values,
                )
            daily_returns.append(
                sim["equity"] / previous_equity - 1 if previous_equity > 0 else 0.0
            )
            curve.append({"date": current_date, "value": rounded(sim["equity"], 2)})
        return curve, daily_returns

    def _apply_backtest_actions(
        self,
        sim: dict[str, Any],
        action_index: int,
        previous_date: str,
        current_date: str,
    ) -> int:
        corporate_actions = sim["corporate_actions"]
        while action_index < len(corporate_actions):
            action = corporate_actions[action_index]
            effective_date = str(action.get("effective_at") or "")[:10]
            if effective_date > current_date:
                break
            action_index += 1
            if effective_date <= previous_date:
                continue
            self._apply_backtest_action(sim, action, effective_date)
        return action_index

    def _apply_backtest_action(
        self,
        sim: dict[str, Any],
        action: dict[str, Any],
        effective_date: str,
    ) -> None:
        units = sim["units"]
        symbol = str(action.get("symbol") or "")
        action_type = str(action.get("action_type") or "")
        if symbol not in units:
            return
        if action_type == "split" and number(action.get("ratio")) > 0:
            units[symbol] *= number(action.get("ratio"))
            sim["applied_actions"].append(
                {
                    "symbol": symbol,
                    "action_type": action_type,
                    "effective_at": effective_date,
                    "ratio": number(action.get("ratio")),
                }
            )
            return
        if action_type in {"dividend", "fund_distribution"} and number(action.get("cash_amount")) >= 0:
            cash_amount = units[symbol] * number(action.get("cash_amount"))
            sim["cash"] += cash_amount
            sim["applied_actions"].append(
                {
                    "symbol": symbol,
                    "action_type": action_type,
                    "effective_at": effective_date,
                    "cash_amount": rounded(cash_amount, 4),
                }
            )
            return
        sim["ignored_actions"].append(
            {
                "symbol": symbol,
                "action_type": action_type,
                "effective_at": effective_date,
                "reason": "unsupported_in_backtest",
            }
        )

    def _backtest_rebalance_step(
        self,
        sim: dict[str, Any],
        series: dict[str, dict[str, float]],
        dates: list[str],
        symbol_list: list[str],
        strategy: str,
        index: int,
        previous_date: str,
        current_date: str,
        current_values: dict[str, float],
    ) -> None:
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
            symbol: sim["equity"] * targets[symbol] for symbol in symbol_list
        }
        traded_notional = sum(
            abs(desired_before_cost[symbol] - current_values[symbol])
            for symbol in symbol_list
        )
        transaction_cost = min(sim["equity"], traded_notional * sim["cost_rate"])
        post_cost_equity = max(0.0, sim["equity"] - transaction_cost)
        sim["turnover_total"] += (
            traded_notional / (2 * sim["equity"]) if sim["equity"] > 0 else 0.0
        )
        sim["rebalance_cost"] += transaction_cost
        sim["units"] = {
            symbol: (
                post_cost_equity * targets[symbol]
                / series[symbol][current_date]
                if series[symbol][current_date] > 0
                else 0.0
            )
            for symbol in symbol_list
        }
        sim["cash"] = 0.0
        sim["equity"] = post_cost_equity

    def _backtest_metrics(
        self, sim: dict[str, Any], dates: list[str]
    ) -> tuple[float, float | None, float | None]:
        daily_returns = sim["daily_returns"]
        volatility = statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) if len(daily_returns) > 1 else 0.0
        years = (len(dates) - 1) / TRADING_DAYS
        annual_return = (
            (sim["equity"] / sim["capital"]) ** (1 / years) - 1
            if years > 0 and sim["equity"] > 0 and sim["capital"] > 0
            else None
        )
        sharpe = (_mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS)) if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0 else None
        return volatility, annual_return, sharpe

    def _backtest_result(
        self,
        series: dict[str, dict[str, float]],
        dates: list[str],
        symbol_list: list[str],
        strategy: str,
        sim: dict[str, Any],
    ) -> dict[str, Any]:
        values = [number(item["value"]) for item in sim["curve"]]
        ending_weights = self._backtest_ending_weights(
            sim, series, dates, symbol_list
        )
        capital = sim["capital"]
        return {
            "ok": True,
            "status": "ready",
            "strategy": strategy,
            "symbols": symbol_list,
            "sample_count": len(dates),
            "initial_capital": rounded(capital, 2),
            "ending_value": rounded(sim["equity"], 2),
            "total_return_percent": rounded((sim["equity"] / capital - 1) * 100, 2),
            "annualized_return_percent": rounded((sim["annual_return"] or 0) * 100, 2) if sim["annual_return"] is not None else None,
            "annualized_volatility_percent": rounded(sim["volatility"] * 100, 2),
            "sharpe_ratio": rounded(sim["sharpe"], 3),
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2),
            "turnover_percent": rounded(sim["turnover_total"] * 100, 2),
            "ending_weights": ending_weights,
            "cost_assumptions": self._backtest_cost_assumptions(sim),
            "methodology": "unit_based_holdings_with_natural_weight_drift",
            "lookahead_protection": "signals use only prices available before each rebalance",
            "corporate_actions": {
                "policy": "approved actions only",
                "applied": sim["applied_actions"],
                "ignored": sim["ignored_actions"],
            },
            "analysis_coverage": {
                "requested_symbols": symbol_list,
                "available_symbols": symbol_list,
                "common_date_count": len(dates),
                "start_date": dates[0],
                "end_date": dates[-1],
            },
            "assumptions": _BACKTEST_ASSUMPTIONS,
            "equity_curve": sim["curve"][-800:],
        }

    def _backtest_ending_weights(
        self,
        sim: dict[str, Any],
        series: dict[str, dict[str, float]],
        dates: list[str],
        symbol_list: list[str],
    ) -> dict[str, Any]:
        ending_values = {
            symbol: sim["units"][symbol] * series[symbol][dates[-1]]
            for symbol in symbol_list
        }
        ending_total = sim["cash"] + sum(ending_values.values())
        return {
            symbol: (
                rounded(ending_values[symbol] / ending_total * 100, 4)
                if ending_total > 0
                else None
            )
            for symbol in symbol_list
        }

    def _backtest_cost_assumptions(
        self, sim: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "fee_percent": sim["fee_percent"],
            "slippage_percent": sim["slippage_percent"],
            "initial_transaction_cost": rounded(sim["initial_transaction_cost"], 2),
            "rebalance_transaction_cost": rounded(sim["rebalance_cost"], 2),
            "total_transaction_cost": rounded(sim["initial_transaction_cost"] + sim["rebalance_cost"], 2),
            "initial_purchase_included": True,
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
        normalized = self._normalized_rebalance_weights(
            symbols, raw_targets, investable_percent, position_cap
        )
        actual_invested_percent = sum(normalized.values())
        orders, estimated_fees = self._rebalance_orders(
            positions, normalized, total, min_trade_value, fee_percent
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

    def _normalized_rebalance_weights(
        self,
        symbols: list[str],
        raw_targets: dict[str, float],
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
        normalized: dict[str, float],
        total: float,
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
