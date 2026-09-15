from __future__ import annotations

import math
import random
import statistics
from typing import Any, Sequence

from ..infrastructure.analytics_repository import (
    TRADING_DAYS,
    _correlation,
    _mean,
    _returns,
    number,
    rounded,
)
from .portfolio_fx import FACTOR_PROXIES
from .portfolio_math import _solve_linear


class PortfolioEngineSimulationMixin:
    def regime_detection(self, state: dict[str, Any]) -> dict[str, Any]:
        risk = self.store.risk(state)
        positions, dataset, symbols, coverage = self._analysis_frame(state, 260)
        if dataset["sample_count"] < 20:
            return {
                "status": "insufficient_history",
                "sample_count": dataset["sample_count"],
                "regime": "unknown",
                "analysis_coverage": coverage,
            }
        weights = self._normalized_position_weights(positions, symbols)
        portfolio = [
            sum(weights[symbol] * dataset["returns"][symbol][index] for symbol in symbols)
            for index in range(dataset["sample_count"])
        ]
        return_20 = math.prod(1 + value for value in portfolio[-20:]) - 1
        return_60 = math.prod(1 + value for value in portfolio[-60:]) - 1 if len(portfolio) >= 60 else return_20
        volatility_20 = statistics.stdev(portfolio[-20:]) * math.sqrt(TRADING_DAYS) if len(portfolio) >= 20 else 0
        if volatility_20 >= 0.30:
            regime = "high_volatility"
            label = "高波動"
        elif return_20 <= -0.08 or return_60 <= -0.15:
            regime = "bear"
            label = "空頭"
        elif return_20 >= 0.06 and return_60 > 0:
            regime = "bull"
            label = "多頭"
        elif abs(return_20) <= 0.025:
            regime = "sideways"
            label = "盤整"
        else:
            regime = "transition"
            label = "轉換期"
        return {
            "status": "ready",
            "regime": regime,
            "label": label,
            "confidence": rounded(min(0.95, 0.5 + abs(return_20) * 2 + volatility_20 * 0.4), 3),
            "return_20d_percent": rounded(return_20 * 100, 2),
            "return_60d_percent": rounded(return_60 * 100, 2),
            "volatility_20d_percent": rounded(volatility_20 * 100, 2),
            "var_95_percent": risk.get("var_95_one_day_percent"),
            "sample_count": len(portfolio),
            "analysis_coverage": coverage,
        }

    def monte_carlo(
        self,
        state: dict[str, Any],
        *,
        simulations: int = 2000,
        horizon_days: int = 252,
        target_return_percent: float = 0,
        seed: int = 73021,
    ) -> dict[str, Any]:
        positions, dataset, symbols, coverage = self._analysis_frame(state, 800)
        if dataset["sample_count"] < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": dataset["sample_count"],
                "analysis_coverage": coverage,
            }
        weights = self._normalized_position_weights(positions, symbols)
        historical = [
            sum(weights[symbol] * dataset["returns"][symbol][index] for symbol in symbols)
            for index in range(dataset["sample_count"])
        ]
        mean = _mean(historical)
        volatility = statistics.stdev(historical)
        current_value = sum(number(item.get("market_value")) for item in positions)
        count = max(200, min(20000, int(simulations)))
        days = max(5, min(2520, int(horizon_days)))
        terminal_returns, drawdowns = self._mc_simulate(mean, volatility, count, days, seed)
        return self._mc_result(
            count,
            days,
            historical,
            current_value,
            terminal_returns,
            drawdowns,
            target_return_percent / 100,
            coverage,
            seed,
        )

    @staticmethod
    def _mc_simulate(
        mean: float,
        volatility: float,
        count: int,
        days: int,
        seed: int,
    ) -> tuple[list[float], list[float]]:
        rng = random.Random(seed)
        terminal_returns = []
        drawdowns = []
        for _ in range(count):
            value = 1.0
            peak = 1.0
            worst = 0.0
            for _day in range(days):
                shock = rng.gauss(mean, volatility)
                value *= max(0.01, 1 + shock)
                peak = max(peak, value)
                worst = min(worst, value / peak - 1)
            terminal_returns.append(value - 1)
            drawdowns.append(worst)
        return terminal_returns, drawdowns

    @staticmethod
    def _mc_result(
        count: int,
        days: int,
        historical: Sequence[float],
        current_value: float,
        terminal_returns: Sequence[float],
        drawdowns: Sequence[float],
        target: float,
        coverage: dict[str, Any],
        seed: int,
    ) -> dict[str, Any]:
        ordered = sorted(terminal_returns)
        percentile = lambda level: ordered[min(len(ordered) - 1, max(0, int(level * (len(ordered) - 1))))]
        return {
            "ok": True,
            "status": "ready",
            "simulations": count,
            "horizon_days": days,
            "sample_count": len(historical),
            "current_value": rounded(current_value, 2),
            "terminal_value": {
                "p05": rounded(current_value * (1 + percentile(0.05)), 2),
                "p50": rounded(current_value * (1 + percentile(0.50)), 2),
                "p95": rounded(current_value * (1 + percentile(0.95)), 2),
            },
            "terminal_return_percent": {
                "p05": rounded(percentile(0.05) * 100, 2),
                "p50": rounded(percentile(0.50) * 100, 2),
                "p95": rounded(percentile(0.95) * 100, 2),
            },
            "probability_of_loss_percent": rounded(sum(value < 0 for value in terminal_returns) / count * 100, 2),
            "target_success_percent": rounded(sum(value >= target for value in terminal_returns) / count * 100, 2),
            "median_max_drawdown_percent": rounded(statistics.median(drawdowns) * 100, 2),
            "methodology": "seeded univariate Gaussian fit to current-weight portfolio returns",
            "assumptions": [
                "Current position weights are held constant for the simulated horizon.",
                "Daily portfolio returns are independent Gaussian draws.",
                "Fat tails, volatility regimes, cash flows, taxes and trading costs are not modeled.",
            ],
            "analysis_coverage": coverage,
            "seed": seed,
        }

    def factor_attribution(self, state: dict[str, Any]) -> dict[str, Any]:
        positions, portfolio_dataset, symbols, coverage = self._analysis_frame(state, 800)
        if portfolio_dataset["sample_count"] < 30:
            return {
                "status": "insufficient_history",
                "sample_count": portfolio_dataset["sample_count"],
                "factors": [],
                "analysis_coverage": coverage,
            }
        weights = self._normalized_position_weights(positions, symbols)
        portfolio_returns = [
            sum(weights[symbol] * portfolio_dataset["returns"][symbol][index] for symbol in symbols)
            for index in range(portfolio_dataset["sample_count"])
        ]
        factor_series, unavailable = self._factor_return_series(len(portfolio_returns))
        if not factor_series:
            return {
                "status": "factor_proxies_missing",
                "sample_count": len(portfolio_returns),
                "factors": [],
                "unavailable_factors": unavailable,
                "analysis_coverage": coverage,
            }
        common, regression = self._factor_regression(factor_series, portfolio_returns)
        if regression is None:
            return {
                "status": "regression_failed",
                "sample_count": common,
                "factors": [],
                "analysis_coverage": coverage,
            }
        coefficients, residual, total, y, names = regression
        factors = self._factor_rows(names, coefficients, factor_series, y, common)
        symbol_contributions = self._factor_symbol_contributions(positions, portfolio_dataset["returns"], weights)
        return {
            "status": "ready",
            "sample_count": common,
            "alpha_annual_percent": rounded(coefficients[0] * TRADING_DAYS * 100, 2),
            "r_squared": rounded(1 - residual / total, 4) if total > 0 else None,
            "factors": factors,
            "symbol_contributions": symbol_contributions,
            "unavailable_factors": unavailable,
            "methodology": "ridge-stabilized proxy factor regression",
            "analysis_coverage": coverage,
        }

    def _factor_return_series(self, portfolio_len: int) -> tuple[dict[str, list[float]], list[str]]:
        factor_series: dict[str, list[float]] = {}
        unavailable = []
        for factor, (long_symbol, short_symbol) in FACTOR_PROXIES.items():
            long_prices = [number(item.get("close")) for item in self.store.price_series(long_symbol, 800)]
            short_prices = [number(item.get("close")) for item in self.store.price_series(short_symbol, 800)] if short_symbol else []
            long_returns = _returns(long_prices)
            if not long_returns:
                unavailable.append(factor)
                continue
            if short_symbol:
                short_returns = _returns(short_prices)
                common = min(len(long_returns), len(short_returns), portfolio_len)
                if common < 30:
                    unavailable.append(factor)
                    continue
                factor_series[factor] = [long_returns[-common + index] - short_returns[-common + index] for index in range(common)]
            else:
                factor_series[factor] = long_returns[-portfolio_len:]
        return factor_series, unavailable

    @staticmethod
    def _factor_regression(
        factor_series: dict[str, list[float]],
        portfolio_returns: Sequence[float],
    ) -> tuple[int, tuple[list[float], float, float, list[float], list[str]] | None]:
        common = min(len(portfolio_returns), *(len(values) for values in factor_series.values()))
        names = list(factor_series)
        x_rows = [[1.0] + [factor_series[name][-common + index] for name in names] for index in range(common)]
        y = portfolio_returns[-common:]
        columns = len(names) + 1
        matrix = [[sum(row[left] * row[right] for row in x_rows) for right in range(columns)] for left in range(columns)]
        for index in range(columns):
            matrix[index][index] += 1e-8
        vector = [sum(row[column] * y[index] for index, row in enumerate(x_rows)) for column in range(columns)]
        coefficients = _solve_linear(matrix, vector)
        if coefficients is None:
            return common, None
        predicted = [sum(coefficients[column] * row[column] for column in range(columns)) for row in x_rows]
        residual = sum((y[index] - predicted[index]) ** 2 for index in range(common))
        total = sum((value - _mean(y)) ** 2 for value in y)
        return common, (coefficients, residual, total, y, names)

    @staticmethod
    def _factor_rows(
        names: Sequence[str],
        coefficients: Sequence[float],
        factor_series: dict[str, list[float]],
        y: Sequence[float],
        common: int,
    ) -> list[dict[str, Any]]:
        factors = []
        for index, name in enumerate(names, start=1):
            contribution = coefficients[index] * _mean(factor_series[name][-common:]) * TRADING_DAYS
            factors.append(
                {
                    "factor": name,
                    "exposure": rounded(coefficients[index], 4),
                    "annual_contribution_percent": rounded(contribution * 100, 2),
                    "correlation": rounded(_correlation(y, factor_series[name][-common:]), 4),
                }
            )
        return factors

    @staticmethod
    def _factor_symbol_contributions(
        positions: Sequence[dict[str, Any]],
        returns: dict[str, list[float]],
        weights: dict[str, float],
    ) -> list[dict[str, Any]]:
        contributions = []
        for position in positions:
            symbol = str(position.get("symbol") or "")
            values = returns.get(symbol, [])
            contribution = weights.get(symbol, 0) * (_mean(values) * TRADING_DAYS if values else 0)
            contributions.append({"symbol": symbol, "contribution_percent": rounded(contribution * 100, 2), "weight_percent": position.get("weight_percent")})
        return sorted(contributions, key=lambda item: abs(number(item.get("contribution_percent"))), reverse=True)
