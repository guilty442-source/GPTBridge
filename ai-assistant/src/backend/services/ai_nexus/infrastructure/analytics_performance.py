from __future__ import annotations

import math
import statistics
from typing import Any

from .analytics_common import (
    DEFAULT_BENCHMARK,
    TRADING_DAYS,
    _correlation,
    _covariance,
    _max_drawdown,
    _mean,
    _percentile,
    _returns,
    _variance,
    number,
    parse_datetime,
    rounded,
    utc_now,
    utc_text,
    xirr,
)


class PerformanceMixin:
    """Performance, risk, and stress test methods."""

    def performance(self, state: dict[str, Any]) -> dict[str, Any]:
        positions = self.current_positions(state)
        current_value = sum(number(item.get("market_value")) for item in positions)
        current_cost = sum(number(item.get("cost_value")) for item in positions)
        ledger = self.ledger_summary()
        with self.connect() as connection:
            snapshots = [
                dict(row)
                for row in connection.execute(
                    "SELECT snapshot_id, observed_at, total_value, total_cost, base_currency, cash_value FROM portfolio_snapshots WHERE total_value > 0 ORDER BY observed_at LIMIT 2000"
                ).fetchall()
            ]
        values = [number(item.get("total_value")) for item in snapshots]
        daily_returns = _returns(values)
        twr = math.prod(1 + value for value in daily_returns) - 1 if daily_returns else None
        cashflows: list[tuple[Any, float]] = []
        for item in ledger["cashflows"]:
            date = parse_datetime(item.get("date"))
            if date is not None:
                cashflows.append((date, number(item.get("amount"))))
        if current_value > 0:
            cashflows.append((utc_now(), current_value))
        irr = xirr(cashflows)
        attribution: dict[str, dict[str, float]] = {}
        for position in positions:
            currency = str(position.get("currency") or "UNKNOWN")
            bucket = attribution.setdefault(currency, {"market_value": 0.0, "cost_value": 0.0, "pnl": 0.0})
            bucket["market_value"] += number(position.get("market_value"))
            bucket["cost_value"] += number(position.get("cost_value"))
            bucket["pnl"] += number(position.get("unrealized_pnl"))
        return {
            "status": "ready" if positions else "empty",
            "methodology": "snapshot_twr_and_transaction_xirr",
            "current_value": rounded(current_value, 2),
            "current_cost": rounded(current_cost, 2),
            "unrealized_pnl": rounded(current_value - current_cost, 2),
            "unrealized_pnl_percent": rounded((current_value / current_cost - 1) * 100, 2) if current_cost > 0 else None,
            "realized_pnl": ledger["realized_pnl"],
            "dividend_income": ledger["dividend_income"],
            "fees_and_taxes": ledger["fees_and_taxes"],
            "twr_percent": rounded(twr * 100, 2) if twr is not None else None,
            "xirr_percent": rounded(irr * 100, 2) if irr is not None else None,
            "annualized_volatility_percent": rounded(statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) * 100, 2) if len(daily_returns) > 1 else None,
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2) if values else None,
            "snapshot_count": len(snapshots),
            "attribution_by_currency": {key: {name: rounded(value, 2) for name, value in bucket.items()} for key, bucket in attribution.items()},
            "equity_curve": [
                {"date": item["observed_at"], "value": rounded(number(item["total_value"]), 2)}
                for item in snapshots[-365:]
            ],
            "positions": positions,
            "ledger": {key: value for key, value in ledger.items() if key != "cashflows"},
        }

    def risk(self, state: dict[str, Any], benchmark: str | None = None) -> dict[str, Any]:
        positions = self.current_positions(state)
        raw_weights = {
            str(item.get("symbol") or ""): number(item.get("weight_percent")) / 100
            for item in positions
            if item.get("weight_percent") is not None
        }
        series = {
            symbol: self.price_series(symbol, 520)
            for symbol in raw_weights
        }
        returns_by_symbol: dict[str, dict[str, float]] = {}
        for symbol, bars in series.items():
            prices_by_date = {
                str(bar["observed_at"])[:10]: number(bar["close"])
                for bar in bars
                if number(bar.get("close")) > 0
            }
            ordered_dates = sorted(prices_by_date)
            returns_by_symbol[symbol] = {
                current: prices_by_date[current] / prices_by_date[previous] - 1
                for previous, current in zip(ordered_dates, ordered_dates[1:])
                if prices_by_date[previous] > 0
            }
        eligible_symbols = sorted(
            symbol for symbol, values in returns_by_symbol.items() if len(values) >= 20
        )
        common_dates = (
            sorted(
                set.intersection(
                    *(set(returns_by_symbol[symbol]) for symbol in eligible_symbols)
                )
            )
            if eligible_symbols
            else []
        )
        covered_weight = sum(raw_weights.get(symbol, 0.0) for symbol in eligible_symbols)
        weights = (
            {
                symbol: raw_weights.get(symbol, 0.0) / covered_weight
                for symbol in eligible_symbols
            }
            if covered_weight > 0
            else {}
        )
        aligned_returns = {
            symbol: [returns_by_symbol[symbol][date] for date in common_dates]
            for symbol in eligible_symbols
        }
        portfolio_returns_by_date = {
            date: sum(
                weights[symbol] * returns_by_symbol[symbol][date]
                for symbol in eligible_symbols
            )
            for date in common_dates
        }
        portfolio_returns = [
            portfolio_returns_by_date[date] for date in common_dates
        ]
        volatility = statistics.stdev(portfolio_returns) * math.sqrt(TRADING_DAYS) if len(portfolio_returns) > 1 else None
        var_cutoff = _percentile(portfolio_returns, 0.05)
        tail = [value for value in portfolio_returns if var_cutoff is not None and value <= var_cutoff]
        values = [1.0]
        for value in portfolio_returns:
            values.append(values[-1] * (1 + value))
        correlation: list[dict[str, Any]] = []
        symbols = eligible_symbols
        for left_index, left in enumerate(symbols):
            for right in symbols[left_index + 1 :]:
                coefficient = _correlation(
                    aligned_returns[left],
                    aligned_returns[right],
                )
                correlation.append(
                    {
                        "left": left,
                        "right": right,
                        "correlation": rounded(coefficient, 4),
                        "sample_count": len(common_dates),
                    }
                )
        benchmark_symbol = str(benchmark or self.get_setting("benchmark", DEFAULT_BENCHMARK) or DEFAULT_BENCHMARK).upper()
        benchmark_bars = self.price_series(benchmark_symbol, 520)
        benchmark_prices = {
            str(item["observed_at"])[:10]: number(item["close"])
            for item in benchmark_bars
            if number(item.get("close")) > 0
        }
        benchmark_dates = sorted(benchmark_prices)
        benchmark_returns_by_date = {
            current: benchmark_prices[current] / benchmark_prices[previous] - 1
            for previous, current in zip(benchmark_dates, benchmark_dates[1:])
            if benchmark_prices[previous] > 0
        }
        beta = None
        aligned_dates = sorted(set(portfolio_returns_by_date) & set(benchmark_returns_by_date))
        if len(aligned_dates) > 1:
            aligned_portfolio = [portfolio_returns_by_date[date] for date in aligned_dates]
            aligned_benchmark = [benchmark_returns_by_date[date] for date in aligned_dates]
            denominator = _variance(aligned_benchmark)
            beta = _covariance(aligned_portfolio, aligned_benchmark) / denominator if denominator > 0 else None
        exposures: dict[str, dict[str, float]] = {"market": {}, "currency": {}, "asset_type": {}}
        for item in positions:
            weight = number(item.get("weight_percent"))
            for dimension in exposures:
                key = str(item.get(dimension) or "UNKNOWN")
                exposures[dimension][key] = exposures[dimension].get(key, 0.0) + weight
        covariance = {
            (left, right): _covariance(aligned_returns[left], aligned_returns[right])
            for left in symbols
            for right in symbols
        }
        portfolio_variance = sum(
            weights[left] * weights[right] * covariance[(left, right)]
            for left in symbols
            for right in symbols
        )
        risk_contributions: list[dict[str, Any]] = []
        for symbol in symbols:
            covariance_with_portfolio = sum(
                covariance[(symbol, other)] * weights[other] for other in symbols
            )
            component_fraction = (
                weights[symbol] * covariance_with_portfolio / portfolio_variance
                if portfolio_variance > 0
                else None
            )
            risk_contributions.append(
                {
                    "symbol": symbol,
                    "weight_percent": rounded(weights[symbol] * 100, 2),
                    "portfolio_weight_percent": rounded(raw_weights.get(symbol, 0) * 100, 2),
                    "risk_contribution_percent": (
                        rounded(component_fraction * 100, 2)
                        if component_fraction is not None
                        else None
                    ),
                    "marginal_volatility_annualized_percent": (
                        rounded(
                            covariance_with_portfolio
                            / math.sqrt(portfolio_variance)
                            * math.sqrt(TRADING_DAYS)
                            * 100,
                            2,
                        )
                        if portfolio_variance > 0
                        else None
                    ),
                    "annualized_volatility_percent": (
                        rounded(
                            math.sqrt(_variance(aligned_returns[symbol]))
                            * math.sqrt(TRADING_DAYS)
                            * 100,
                            2,
                        )
                        if len(aligned_returns[symbol]) > 1
                        else None
                    ),
                }
            )
        total_market_value = sum(number(item.get("market_value")) for item in positions)
        analyzed_market_value = sum(
            number(item.get("market_value"))
            for item in positions
            if str(item.get("symbol") or "") in eligible_symbols
        )
        excluded_symbols = sorted(set(raw_weights) - set(eligible_symbols))
        return {
            "status": "ready" if len(portfolio_returns) >= 20 else "insufficient_history",
            "sample_count": len(portfolio_returns),
            "annualized_volatility_percent": rounded(volatility * 100, 2) if volatility is not None else None,
            "beta": rounded(beta, 1),
            "var_95_one_day_percent": rounded(-(var_cutoff or 0) * 100, 2) if var_cutoff is not None else None,
            "cvar_95_one_day_percent": rounded(-_mean(tail) * 100, 2) if tail else None,
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2) if portfolio_returns else None,
            "correlations": sorted(correlation, key=lambda item: abs(number(item.get("correlation"))), reverse=True)[:100],
            "risk_contributions": sorted(risk_contributions, key=lambda item: number(item.get("risk_contribution_percent")), reverse=True),
            "exposures": {dimension: {key: rounded(value, 2) for key, value in values.items()} for dimension, values in exposures.items()},
            "benchmark": benchmark_symbol,
            "weight_methodology": "current_weights_proxy",
            "risk_contribution_methodology": "euler_marginal_contribution_from_covariance",
            "analysis_coverage": {
                "position_count": len(positions),
                "analyzed_position_count": len(eligible_symbols),
                "excluded_symbols": excluded_symbols,
                "market_value_percent": (
                    rounded(analyzed_market_value / total_market_value * 100, 2)
                    if total_market_value > 0
                    else None
                ),
                "requires_common_dates": True,
            },
            "limitations": [
                "Historical holdings are unavailable; current position weights are applied as an explicit proxy.",
                "Positions without at least 20 returns are excluded and remaining weights are renormalized.",
            ],
        }

    def stress_test(self, state: dict[str, Any], scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        positions = self.current_positions(state)
        configured = scenarios or [
            {"name": "大盤急跌", "market_shocks": {"*": -15}},
            {"name": "科技修正", "asset_type_shocks": {"STOCK": -10, "ETF": -7}},
            {"name": "美元升值", "currency_shocks": {"USD": 5, "TWD": -2}},
            {"name": "流動性壓力", "symbol_shocks": {}, "market_shocks": {"CRYPTO": -25, "*": -8}},
        ]
        total = sum(number(item.get("market_value")) for item in positions)
        results: list[dict[str, Any]] = []
        for scenario in configured:
            loss = 0.0
            impacts = []
            for position in positions:
                symbol = str(position.get("symbol") or "")
                market = str(position.get("market") or "")
                asset_type = str(position.get("asset_type") or "").upper()
                currency = str(position.get("currency") or "")
                symbol_shocks = scenario.get("symbol_shocks", {})
                market_shocks = scenario.get("market_shocks", {})
                asset_shocks = scenario.get("asset_type_shocks", {})
                currency_shocks = scenario.get("currency_shocks", {})
                shock = number(symbol_shocks.get(symbol), number(market_shocks.get(market), number(market_shocks.get("*"))))
                shock += number(asset_shocks.get(asset_type))
                shock += number(currency_shocks.get(currency))
                impact = number(position.get("market_value")) * shock / 100
                loss += impact
                impacts.append({"symbol": symbol, "shock_percent": rounded(shock, 2), "impact": rounded(impact, 2)})
            results.append(
                {
                    "name": str(scenario.get("name") or "自訂情境"),
                    "impact": rounded(loss, 2),
                    "impact_percent": rounded(loss / total * 100, 2) if total > 0 else None,
                    "projected_value": rounded(total + loss, 2),
                    "largest_impacts": sorted(impacts, key=lambda item: abs(number(item.get("impact"))), reverse=True)[:8],
                }
            )
        return {"status": "ready" if positions else "empty", "current_value": rounded(total, 2), "scenarios": results}
