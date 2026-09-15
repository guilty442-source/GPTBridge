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
)


class AnalyticsStoreBacktestMixin:
    """Backtest, rebalance simulation, and calibration methods."""

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
        cost_rate = (max(0.0, fee_percent) + max(0.0, slippage_percent)) / 100
        run = self._execute_backtest(symbol_list, series, dates, strategy, capital, cost_rate)
        return self._backtest_result(
            run,
            symbol_list=symbol_list,
            series=series,
            dates=dates,
            strategy=strategy,
            capital=capital,
            fee_percent=fee_percent,
            slippage_percent=slippage_percent,
        )

    def _execute_backtest(
        self,
        symbol_list: list[str],
        series: dict[str, dict[str, float]],
        dates: list[str],
        strategy: str,
        capital: float,
        cost_rate: float,
    ) -> dict[str, Any]:
        run = self._backtest_initial_state(
            symbol_list,
            series,
            dates,
            capital,
            cost_rate,
        )
        run["corporate_actions"] = self._approved_corporate_actions(symbol_list)
        run["applied_actions"] = []
        run["ignored_actions"] = []
        run["action_index"] = 0
        for index in range(1, len(dates)):
            self._backtest_day(
                run,
                index,
                dates,
                symbol_list,
                series,
                strategy,
                cost_rate,
            )
        return run

    def _backtest_initial_state(
        self,
        symbol_list: list[str],
        series: dict[str, dict[str, float]],
        dates: list[str],
        capital: float,
        cost_rate: float,
    ) -> dict[str, Any]:
        target_weights = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
        units = {
            symbol: (capital * target_weights[symbol])
            / (series[symbol][dates[0]] * (1 + cost_rate))
            for symbol in symbol_list
        }
        equity = sum(
            units[symbol] * series[symbol][dates[0]] for symbol in symbol_list
        )
        return {
            "units": units,
            "cash": 0.0,
            "equity": equity,
            "curve": [{"date": dates[0], "value": rounded(equity, 2)}],
            "daily_returns": [],
            "turnover_total": 0.0,
            "rebalance_cost": 0.0,
            "initial_transaction_cost": sum(
                units[symbol] * series[symbol][dates[0]] * cost_rate
                for symbol in symbol_list
            ),
            "target_weights": target_weights,
        }

    def _backtest_day(
        self,
        run: dict[str, Any],
        index: int,
        dates: list[str],
        symbol_list: list[str],
        series: dict[str, dict[str, float]],
        strategy: str,
        cost_rate: float,
    ) -> None:
        previous_date, current_date = dates[index - 1], dates[index]
        run["action_index"], run["cash"] = self._apply_due_actions(
            run["corporate_actions"],
            run["action_index"],
            previous_date,
            current_date,
            run["units"],
            run["cash"],
            run["applied_actions"],
            run["ignored_actions"],
        )
        previous_equity = run["equity"]
        current_values = {
            symbol: run["units"][symbol] * series[symbol][current_date]
            for symbol in symbol_list
        }
        run["equity"] = run["cash"] + sum(current_values.values())
        if strategy in {"equal_weight", "momentum"} and index % 21 == 0:
            step = self._rebalance_step(
                strategy,
                index,
                dates,
                symbol_list,
                series,
                run["equity"],
                current_values,
                cost_rate,
            )
            run["units"] = step["units"]
            run["cash"] = 0.0
            run["equity"] = step["equity"]
            run["target_weights"] = step["targets"]
            run["turnover_total"] += step["turnover"]
            run["rebalance_cost"] += step["cost"]
        run["daily_returns"].append(
            run["equity"] / previous_equity - 1 if previous_equity > 0 else 0.0
        )
        run["curve"].append(
            {"date": current_date, "value": rounded(run["equity"], 2)}
        )

    def _approved_corporate_actions(
        self,
        symbol_list: list[str],
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

    def _apply_due_actions(
        self,
        corporate_actions: list[dict[str, Any]],
        action_index: int,
        previous_date: str,
        current_date: str,
        units: dict[str, float],
        cash: float,
        applied_actions: list[dict[str, Any]],
        ignored_actions: list[dict[str, Any]],
    ) -> tuple[int, float]:
        while action_index < len(corporate_actions):
            action = corporate_actions[action_index]
            effective_date = str(action.get("effective_at") or "")[:10]
            if effective_date > current_date:
                break
            action_index += 1
            if effective_date <= previous_date:
                continue
            cash = self._apply_corporate_action(
                action,
                effective_date,
                units,
                cash,
                applied_actions,
                ignored_actions,
            )
        return action_index, cash

    def _apply_corporate_action(
        self,
        action: dict[str, Any],
        effective_date: str,
        units: dict[str, float],
        cash: float,
        applied_actions: list[dict[str, Any]],
        ignored_actions: list[dict[str, Any]],
    ) -> float:
        symbol = str(action.get("symbol") or "")
        action_type = str(action.get("action_type") or "")
        if symbol not in units:
            return cash
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
        return cash

    def _rebalance_step(
        self,
        strategy: str,
        index: int,
        dates: list[str],
        symbol_list: list[str],
        series: dict[str, dict[str, float]],
        equity: float,
        current_values: dict[str, float],
        cost_rate: float,
    ) -> dict[str, Any]:
        if strategy == "equal_weight":
            targets = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
        else:
            targets = self._momentum_targets(index, dates, symbol_list, series)
        desired_before_cost = {
            symbol: equity * targets[symbol] for symbol in symbol_list
        }
        traded_notional = sum(
            abs(desired_before_cost[symbol] - current_values[symbol])
            for symbol in symbol_list
        )
        transaction_cost = min(equity, traded_notional * cost_rate)
        post_cost_equity = max(0.0, equity - transaction_cost)
        turnover = traded_notional / (2 * equity) if equity > 0 else 0.0
        units = {
            symbol: (
                post_cost_equity * targets[symbol]
                / series[symbol][dates[index]]
                if series[symbol][dates[index]] > 0
                else 0.0
            )
            for symbol in symbol_list
        }
        return {
            "units": units,
            "equity": post_cost_equity,
            "targets": targets,
            "turnover": turnover,
            "cost": transaction_cost,
        }

    def _momentum_targets(
        self,
        index: int,
        dates: list[str],
        symbol_list: list[str],
        series: dict[str, dict[str, float]],
    ) -> dict[str, float]:
        lookback = max(0, index - 60)
        ranked = sorted(
            symbol_list,
            key=lambda symbol: series[symbol][dates[index - 1]] / series[symbol][dates[lookback]] - 1,
            reverse=True,
        )
        selected = set(ranked[: max(1, math.ceil(len(ranked) / 2))])
        return {symbol: (1 / len(selected) if symbol in selected else 0.0) for symbol in symbol_list}

    def _backtest_result(
        self,
        run: dict[str, Any],
        *,
        symbol_list: list[str],
        series: dict[str, dict[str, float]],
        dates: list[str],
        strategy: str,
        capital: float,
        fee_percent: float,
        slippage_percent: float,
    ) -> dict[str, Any]:
        curve = run["curve"]
        equity = run["equity"]
        values = [number(item["value"]) for item in curve]
        stats = self._backtest_stats(run, dates, capital)
        ending_values = {
            symbol: run["units"][symbol] * series[symbol][dates[-1]]
            for symbol in symbol_list
        }
        ending_total = run["cash"] + sum(ending_values.values())
        return {
            "ok": True,
            "status": "ready",
            "strategy": strategy,
            "symbols": symbol_list,
            "sample_count": len(dates),
            **self._backtest_performance_fields(
                run,
                stats,
                values,
                symbol_list,
                ending_values,
                ending_total,
                capital,
            ),
            "cost_assumptions": self._backtest_costs(run, fee_percent, slippage_percent),
            "methodology": "unit_based_holdings_with_natural_weight_drift",
            "lookahead_protection": "signals use only prices available before each rebalance",
            **self._backtest_disclosure_fields(run, symbol_list, dates, curve),
        }

    def _backtest_disclosure_fields(
        self,
        run: dict[str, Any],
        symbol_list: list[str],
        dates: list[str],
        curve: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "corporate_actions": {
                "policy": "approved actions only",
                "applied": run["applied_actions"],
                "ignored": run["ignored_actions"],
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

    def _backtest_performance_fields(
        self,
        run: dict[str, Any],
        stats: dict[str, Any],
        values: list[float],
        symbol_list: list[str],
        ending_values: dict[str, float],
        ending_total: float,
        capital: float,
    ) -> dict[str, Any]:
        equity = run["equity"]
        return {
            "initial_capital": rounded(capital, 2),
            "ending_value": rounded(equity, 2),
            "total_return_percent": rounded((equity / capital - 1) * 100, 2),
            "annualized_return_percent": rounded((stats["annual_return"] or 0) * 100, 2) if stats["annual_return"] is not None else None,
            "annualized_volatility_percent": rounded(stats["volatility"] * 100, 2),
            "sharpe_ratio": rounded(stats["sharpe"], 3),
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2),
            "turnover_percent": rounded(run["turnover_total"] * 100, 2),
            "ending_weights": self._ending_weights(
                symbol_list,
                ending_values,
                ending_total,
            ),
        }

    def _ending_weights(
        self,
        symbol_list: list[str],
        ending_values: dict[str, float],
        ending_total: float,
    ) -> dict[str, Any]:
        return {
            symbol: (
                rounded(ending_values[symbol] / ending_total * 100, 4)
                if ending_total > 0
                else None
            )
            for symbol in symbol_list
        }

    def _backtest_stats(
        self,
        run: dict[str, Any],
        dates: list[str],
        capital: float,
    ) -> dict[str, Any]:
        daily_returns = run["daily_returns"]
        equity = run["equity"]
        volatility = statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) if len(daily_returns) > 1 else 0.0
        years = (len(dates) - 1) / TRADING_DAYS
        annual_return = (
            (equity / capital) ** (1 / years) - 1
            if years > 0 and equity > 0 and capital > 0
            else None
        )
        sharpe = (_mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS)) if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0 else None
        return {
            "volatility": volatility,
            "annual_return": annual_return,
            "sharpe": sharpe,
        }

    def _backtest_costs(
        self,
        run: dict[str, Any],
        fee_percent: float,
        slippage_percent: float,
    ) -> dict[str, Any]:
        return {
            "fee_percent": fee_percent,
            "slippage_percent": slippage_percent,
            "initial_transaction_cost": rounded(run["initial_transaction_cost"], 2),
            "rebalance_transaction_cost": rounded(run["rebalance_cost"], 2),
            "total_transaction_cost": rounded(run["initial_transaction_cost"] + run["rebalance_cost"], 2),
            "initial_purchase_included": True,
        }

__all__ = ['AnalyticsStoreBacktestMixin']
