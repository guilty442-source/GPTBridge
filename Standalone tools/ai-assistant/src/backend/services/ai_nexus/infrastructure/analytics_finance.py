from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Sequence

TRADING_DAYS = 252
DEFAULT_BENCHMARK = "^GSPC"

def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _variance(values: Sequence[float]) -> float:
    return statistics.variance(values) if len(values) > 1 else 0.0


def _covariance(left: Sequence[float], right: Sequence[float]) -> float:
    count = min(len(left), len(right))
    if count < 2:
        return 0.0
    left_values = list(left[-count:])
    right_values = list(right[-count:])
    left_mean = _mean(left_values)
    right_mean = _mean(right_values)
    return sum(
        (left_values[index] - left_mean) * (right_values[index] - right_mean)
        for index in range(count)
    ) / (count - 1)


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    covariance = _covariance(left, right)
    denominator = math.sqrt(_variance(left) * _variance(right))
    return covariance / denominator if denominator > 0 else None


def _returns(values: Sequence[float]) -> list[float]:
    output: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0:
            output.append(current / previous - 1)
    return output


def _max_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(math.floor(percentile * (len(ordered) - 1)))))
    return ordered[position]


def _annualized_return(values: Sequence[float]) -> float | None:
    if len(values) < 2 or values[0] <= 0 or values[-1] <= 0:
        return None
    years = (len(values) - 1) / TRADING_DAYS
    return (values[-1] / values[0]) ** (1 / years) - 1 if years > 0 else None


def _xnpv(rate: float, cashflows: Sequence[tuple[datetime, float]]) -> float:
    if not cashflows:
        return 0.0
    origin = cashflows[0][0]
    return sum(
        amount / ((1 + rate) ** max(0.0, (date - origin).total_seconds() / 31_557_600))
        for date, amount in cashflows
    )


def xirr(cashflows: Sequence[tuple[datetime, float]]) -> float | None:
    ordered = sorted(cashflows, key=lambda item: item[0])
    if not ordered or not any(amount < 0 for _, amount in ordered) or not any(amount > 0 for _, amount in ordered):
        return None
    low, high = -0.9999, 100.0
    low_value, high_value = _xnpv(low, ordered), _xnpv(high, ordered)
    if low_value * high_value > 0:
        return None
    for _ in range(160):
        middle = (low + high) / 2
        value = _xnpv(middle, ordered)
        if abs(value) < 1e-8:
            return middle
        if value * low_value > 0:
            low, low_value = middle, value
        else:
            high, high_value = middle, value
    return (low + high) / 2



__all__ = ['TRADING_DAYS', 'DEFAULT_BENCHMARK', '_mean', '_variance', '_covariance', '_correlation', '_returns', '_max_drawdown', '_percentile', '_annualized_return', '_xnpv', 'xirr']
