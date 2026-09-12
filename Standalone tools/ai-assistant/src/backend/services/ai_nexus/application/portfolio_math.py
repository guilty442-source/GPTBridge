from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from ..infrastructure.analytics_repository import (
    TRADING_DAYS,
    _mean,
    _percentile,
    number,
    rounded,
)


def _json_hash(value: Any) -> str:
    import json

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_weights(
    raw: dict[str, float],
    *,
    max_weight: float,
    min_weight: float = 0.0,
) -> dict[str, float]:
    symbols = list(raw)
    if not symbols:
        return {}
    cap = max(1 / len(symbols), min(1.0, max(0.01, max_weight)))
    floor = max(0.0, min(cap, min_weight))
    weights = {symbol: max(floor, number(raw[symbol])) for symbol in symbols}
    for _ in range(40):
        total = sum(weights.values())
        if total <= 0:
            weights = {symbol: 1 / len(symbols) for symbol in symbols}
        else:
            weights = {symbol: value / total for symbol, value in weights.items()}
        excess = sum(max(0.0, value - cap) for value in weights.values())
        weights = {symbol: min(cap, value) for symbol, value in weights.items()}
        if excess <= 1e-10:
            break
        available = [symbol for symbol, value in weights.items() if value < cap - 1e-10]
        if not available:
            break
        room = sum(cap - weights[symbol] for symbol in available)
        for symbol in available:
            weights[symbol] += excess * (cap - weights[symbol]) / room if room > 0 else 0
    total = sum(weights.values())
    return {symbol: value / total for symbol, value in weights.items()} if total > 0 else weights


def _portfolio_stats(
    weights: dict[str, float],
    means: dict[str, float],
    covariance: dict[tuple[str, str], float],
    samples: dict[str, list[float]],
) -> dict[str, float | None]:
    expected_daily = sum(weights.get(symbol, 0) * means.get(symbol, 0) for symbol in weights)
    variance = sum(
        weights.get(left, 0) * weights.get(right, 0) * covariance.get((left, right), 0)
        for left in weights
        for right in weights
    )
    volatility = math.sqrt(max(0.0, variance))
    common_count = min((len(samples.get(symbol, [])) for symbol in weights), default=0)
    portfolio_returns = [
        sum(weights[symbol] * samples[symbol][-common_count + index] for symbol in weights)
        for index in range(common_count)
    ] if common_count else []
    cutoff = _percentile(portfolio_returns, 0.05)
    tail = [value for value in portfolio_returns if cutoff is not None and value <= cutoff]
    annual_return = expected_daily * TRADING_DAYS
    annual_volatility = volatility * math.sqrt(TRADING_DAYS)
    return {
        "expected_return_percent": rounded(annual_return * 100, 2),
        "volatility_percent": rounded(annual_volatility * 100, 2),
        "sharpe_ratio": rounded(annual_return / annual_volatility, 4) if annual_volatility > 0 else None,
        "cvar_95_percent": rounded(max(0.0, -_mean(tail)) * 100, 2) if tail else None,
    }


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    count = len(vector)
    augmented = [list(matrix[index]) + [vector[index]] for index in range(count)]
    for pivot in range(count):
        best = max(range(pivot, count), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) < 1e-12:
            return None
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        scale = augmented[pivot][pivot]
        augmented[pivot] = [value / scale for value in augmented[pivot]]
        for row in range(count):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            augmented[row] = [
                augmented[row][column] - factor * augmented[pivot][column]
                for column in range(count + 1)
            ]
    return [augmented[index][-1] for index in range(count)]
