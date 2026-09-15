from __future__ import annotations

import math
import random
from typing import Any, Sequence

from ..infrastructure.analytics_repository import (
    TRADING_DAYS,
    _covariance,
    _mean,
    _variance,
    number,
    rounded,
)
from .portfolio_math import _portfolio_stats, _project_weights


class PortfolioEngineAnalysisMixin:
    def _symbol_dated_returns(self, symbol: str, limit: int) -> dict[str, float]:
        prices_by_date: dict[str, tuple[float, bool]] = {}
        for item in self.store.price_series(symbol, limit):
            close = number(item.get("close"))
            date = str(item.get("observed_at") or "")[:10]
            verified = bool(item.get("verified"))
            previous = prices_by_date.get(date)
            if date and close > 0 and (previous is None or verified and not previous[1]):
                prices_by_date[date] = (close, verified)
        dates = sorted(prices_by_date)
        return {
            current: prices_by_date[current][0] / prices_by_date[previous][0] - 1
            for previous, current in zip(dates, dates[1:])
            if prices_by_date[previous][0] > 0
        }

    def _returns_dataset(self, symbols: Sequence[str], limit: int = 800) -> dict[str, Any]:
        returns_by_date: dict[str, dict[str, float]] = {}
        requested = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()))
        for symbol in requested:
            dated_returns = self._symbol_dated_returns(symbol, limit)
            if len(dated_returns) >= 20:
                returns_by_date[str(symbol)] = dated_returns
        eligible_symbols = list(returns_by_date)
        common_dates = (
            sorted(
                set.intersection(
                    *(set(returns_by_date[symbol]) for symbol in eligible_symbols)
                )
            )
            if eligible_symbols
            else []
        )
        returns = {
            symbol: [returns_by_date[symbol][date] for date in common_dates]
            for symbol in eligible_symbols
        }
        symbols = list(returns)
        means = {symbol: _mean(values) for symbol, values in returns.items()}
        covariance = {
            (left, right): _covariance(returns[left], returns[right])
            for left in symbols
            for right in symbols
        }
        return {
            "requested_symbols": requested,
            "symbols": symbols,
            "excluded_symbols": sorted(set(requested) - set(symbols)),
            "returns": returns,
            "means": means,
            "covariance": covariance,
            "sample_count": len(common_dates),
            "common_dates": common_dates,
        }

    def _analysis_positions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        positions = self.store.current_positions(state)
        return sorted(positions, key=lambda item: number(item.get("market_value")), reverse=True)

    @staticmethod
    def _analysis_coverage(
        positions: Sequence[dict[str, Any]],
        analyzed_symbols: Sequence[str],
        dataset: dict[str, Any],
    ) -> dict[str, Any]:
        analyzed = set(analyzed_symbols)
        total_value = sum(number(item.get("market_value")) for item in positions)
        analyzed_value = sum(
            number(item.get("market_value"))
            for item in positions
            if str(item.get("symbol") or "") in analyzed
        )
        requested_symbols = [
            str(item.get("symbol") or "")
            for item in positions
            if str(item.get("symbol") or "")
        ]
        return {
            "position_count": len(positions),
            "requested_symbol_count": len(set(requested_symbols)),
            "analyzed_symbol_count": len(analyzed),
            "analyzed_symbols": sorted(analyzed),
            "excluded_symbols": sorted(set(requested_symbols) - analyzed),
            "market_value_percent": (
                rounded(analyzed_value / total_value * 100, 2)
                if total_value > 0
                else None
            ),
            "common_date_count": int(dataset.get("sample_count") or 0),
            "date_alignment": "intersection_of_observed_trading_dates",
            "position_cap_applied": False,
        }

    @staticmethod
    def _normalized_position_weights(positions: Sequence[dict[str, Any]], symbols: Sequence[str]) -> dict[str, float]:
        raw = {
            str(item.get("symbol") or ""): max(0.0, number(item.get("weight_percent")) / 100)
            for item in positions
            if str(item.get("symbol") or "") in symbols
        }
        total = sum(raw.values())
        return {symbol: raw.get(symbol, 0) / total for symbol in symbols} if total > 0 else {symbol: 1 / len(symbols) for symbol in symbols}

    def _analysis_frame(
        self,
        state: dict[str, Any],
        limit: int = 800,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], dict[str, Any]]:
        positions = self._analysis_positions(state)
        dataset = self._returns_dataset([str(item.get("symbol") or "") for item in positions], limit)
        symbols = dataset["symbols"]
        coverage = self._analysis_coverage(positions, symbols, dataset)
        return positions, dataset, symbols, coverage

    def optimize_portfolio(
        self,
        state: dict[str, Any],
        *,
        method: str = "risk_parity",
        max_position_percent: float = 35,
        max_turnover_percent: float = 40,
        views: dict[str, float] | None = None,
        seed: int = 73021,
    ) -> dict[str, Any]:
        positions, dataset, symbols, coverage = self._analysis_frame(state)
        if len(symbols) < 2 or dataset["sample_count"] < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": dataset["sample_count"],
                "weights": {},
                "analysis_coverage": coverage,
            }
        means: dict[str, float] = dict(dataset["means"])
        covariance: dict[tuple[str, str], float] = dict(dataset["covariance"])
        samples: dict[str, list[float]] = dict(dataset["returns"])
        current = self._normalized_position_weights(positions, symbols)
        cap = max_position_percent / 100
        if cap * len(symbols) < 1 - 1e-9:
            return {
                "ok": False,
                "status": "infeasible_constraints",
                "sample_count": dataset["sample_count"],
                "weights": {},
                "message": f"單一部位上限至少需為 {100 / len(symbols):.2f}%",
                "analysis_coverage": coverage,
            }
        method = str(method or "risk_parity")
        raw = self._raw_optimized_weights(method, symbols, means, covariance, samples, current, cap, views, seed)
        optimized = _project_weights(raw, max_weight=cap)
        optimized, turnover = self._apply_turnover_cap(optimized, current, symbols, cap, max_turnover_percent)
        stats = _portfolio_stats(optimized, means, covariance, samples)
        return self._optimize_result(
            method,
            dataset["sample_count"],
            optimized,
            current,
            turnover,
            max_position_percent,
            max_turnover_percent,
            stats,
            coverage,
        )

    @staticmethod
    def _optimize_result(
        method: str,
        sample_count: int,
        optimized: dict[str, float],
        current: dict[str, float],
        turnover: float,
        max_position_percent: float,
        max_turnover_percent: float,
        stats: dict[str, Any],
        coverage: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "draft",
            "method": method,
            "sample_count": sample_count,
            "weights": {symbol: rounded(value * 100, 2) for symbol, value in optimized.items()},
            "current_weights": {symbol: rounded(value * 100, 2) for symbol, value in current.items()},
            "turnover_percent": rounded(turnover * 100, 2),
            "max_position_percent": max_position_percent,
            "max_turnover_percent": max_turnover_percent,
            "statistics": stats,
            "execution_policy": "simulation_only_human_approval_required",
            "assumptions": ["historical daily returns", "long only", "weights sum to 100%", "no automatic order submission"],
            "analysis_coverage": coverage,
        }

    def _raw_optimized_weights(
        self,
        method: str,
        symbols: Sequence[str],
        means: dict[str, float],
        covariance: dict[tuple[str, str], float],
        samples: dict[str, list[float]],
        current: dict[str, float],
        cap: float,
        views: dict[str, float] | None,
        seed: int,
    ) -> dict[str, float]:
        if method == "risk_parity":
            return {symbol: 1 / max(1e-8, math.sqrt(_variance(samples[symbol]))) for symbol in symbols}
        if method in {"minimum_variance", "max_sharpe", "black_litterman"}:
            return self._gradient_weights(method, symbols, means, covariance, current, cap, views)
        if method == "cvar":
            return self._cvar_weights(symbols, means, covariance, samples, cap, seed)
        raise ValueError("unsupported optimization method")

    @staticmethod
    def _gradient_weights(
        method: str,
        symbols: Sequence[str],
        means: dict[str, float],
        covariance: dict[tuple[str, str], float],
        current: dict[str, float],
        cap: float,
        views: dict[str, float] | None,
    ) -> dict[str, float]:
        if method == "black_litterman":
            for symbol, annual_view in (views or {}).items():
                if symbol in means:
                    means[symbol] = means[symbol] * 0.65 + number(annual_view) / 100 / TRADING_DAYS * 0.35
        raw = dict(current)
        learning_rate = 0.12
        for _ in range(500):
            portfolio_mean = sum(raw[symbol] * means[symbol] for symbol in symbols)
            portfolio_variance = sum(raw[left] * raw[right] * covariance[(left, right)] for left in symbols for right in symbols)
            portfolio_volatility = math.sqrt(max(1e-12, portfolio_variance))
            gradient = {}
            for symbol in symbols:
                marginal_variance = sum(raw[other] * covariance[(symbol, other)] for other in symbols)
                if method == "minimum_variance":
                    gradient[symbol] = 2 * marginal_variance
                else:
                    gradient[symbol] = -(
                        means[symbol] * portfolio_volatility
                        - portfolio_mean * marginal_variance / portfolio_volatility
                    ) / max(1e-12, portfolio_variance)
            raw = _project_weights(
                {symbol: raw[symbol] - learning_rate * gradient[symbol] for symbol in symbols},
                max_weight=cap,
            )
            learning_rate *= 0.995
        return raw

    @staticmethod
    def _cvar_weights(
        symbols: Sequence[str],
        means: dict[str, float],
        covariance: dict[tuple[str, str], float],
        samples: dict[str, list[float]],
        cap: float,
        seed: int,
    ) -> dict[str, float]:
        rng = random.Random(seed)
        candidates = []
        for _ in range(3500):
            draws = {symbol: rng.expovariate(1.0) for symbol in symbols}
            weights = _project_weights(draws, max_weight=cap)
            stats = _portfolio_stats(weights, means, covariance, samples)
            candidates.append((number(stats.get("cvar_95_percent"), 999), weights, stats))
        _score, raw, _stats = min(candidates, key=lambda item: item[0])
        return raw

    @staticmethod
    def _apply_turnover_cap(
        optimized: dict[str, float],
        current: dict[str, float],
        symbols: Sequence[str],
        cap: float,
        max_turnover_percent: float,
    ) -> tuple[dict[str, float], float]:
        turnover = sum(abs(optimized.get(symbol, 0) - current.get(symbol, 0)) for symbol in symbols) / 2
        turnover_cap = max(0.0, max_turnover_percent / 100)
        if turnover > turnover_cap and turnover > 0:
            blend = turnover_cap / turnover
            optimized = _project_weights(
                {
                    symbol: current.get(symbol, 0) + (optimized.get(symbol, 0) - current.get(symbol, 0)) * blend
                    for symbol in symbols
                },
                max_weight=cap,
            )
            turnover = sum(abs(optimized.get(symbol, 0) - current.get(symbol, 0)) for symbol in symbols) / 2
        return optimized, turnover
