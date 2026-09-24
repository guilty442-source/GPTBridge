"""Deterministic technical indicators — the ONLY source of indicator values.

AI interprets these numbers; it never fabricates them. Pure Decimal-free
float math is fine for indicators (analysis, not settlement) — but every
function is pure and deterministic over the input series.
"""

from __future__ import annotations

from typing import Sequence

__all__ = [
    "sma", "ema", "rsi", "macd", "volatility", "max_drawdown",
    "relative_strength", "turnover", "returns", "IndicatorSet",
]


def sma(values: Sequence[float], period: int) -> list[float]:
    """Simple moving average; leading positions with < period points are nan-free
    — we emit only full windows, aligning output to the last len-period+1 inputs."""
    if period <= 0 or len(values) < period:
        return []
    out: list[float] = []
    window = sum(values[:period])
    out.append(window / period)
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out.append(window / period)
    return out


def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0 or not values:
        return []
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(out[-1] + k * (float(v) - out[-1]))
    return out


def returns(values: Sequence[float]) -> list[float]:
    return [
        b / a - 1.0
        for a, b in zip(values, values[1:])
        if a
    ]


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder RSI over the full series; None when insufficient data."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, float | None]:
    """MACD line / signal / histogram (latest values)."""
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s for f, s in zip(ef, es)]
    sig = ema(line, signal)
    return {
        "macd": line[-1],
        "signal": sig[-1],
        "histogram": line[-1] - sig[-1],
    }


def volatility(closes: Sequence[float], annualize: bool = True) -> float | None:
    rets = returns(closes)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol = var ** 0.5
    return vol * (252 ** 0.5) if annualize else vol


def max_drawdown(closes: Sequence[float]) -> float | None:
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for v in closes:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def relative_strength(
    closes: Sequence[float], benchmark: Sequence[float]
) -> float | None:
    """Return differential vs benchmark over the common window."""
    n = min(len(closes), len(benchmark))
    if n < 2 or closes[-n] == 0 or benchmark[-n] == 0:
        return None
    return (closes[-1] / closes[-n] - 1.0) - (benchmark[-1] / benchmark[-n] - 1.0)


def turnover(volumes: Sequence[float], prices: Sequence[float]) -> list[float]:
    """Per-bar 成交金額 approximation: volume × price."""
    return [v * p for v, p in zip(volumes, prices)]


class IndicatorSet:
    """Bundle of all deterministic indicators for one series."""

    def __init__(self, closes: Sequence[float],
                 volumes: Sequence[float] | None = None,
                 benchmark: Sequence[float] | None = None) -> None:
        c = [float(x) for x in closes]
        self.count = len(c)
        self.last = c[-1] if c else None
        for p in (5, 20, 50, 200):
            s = sma(c, p)
            setattr(self, f"ma{p}", s[-1] if s else None)
        self.rsi14 = rsi(c, 14)
        m = macd(c)
        self.macd, self.macd_signal, self.macd_hist = (
            m["macd"], m["signal"], m["histogram"])
        self.volatility = volatility(c)
        self.max_drawdown = max_drawdown(c)
        self.relative_strength = (
            relative_strength(c, [float(b) for b in benchmark])
            if benchmark else None
        )
        self.turnover = (
            turnover([float(v) for v in volumes], c) if volumes else []
        )
        self.cum_return = (
            c[-1] / c[0] - 1.0 if len(c) >= 2 and c[0] else None
        )

    def to_dict(self) -> dict[str, float | None]:
        return {
            "count": self.count, "last": self.last,
            "ma5": self.ma5, "ma20": self.ma20,
            "ma50": self.ma50, "ma200": self.ma200,
            "rsi14": self.rsi14,
            "macd": self.macd, "macd_signal": self.macd_signal,
            "macd_histogram": self.macd_hist,
            "volatility_annual": self.volatility,
            "max_drawdown": self.max_drawdown,
            "relative_strength": self.relative_strength,
            "cumulative_return": self.cum_return,
            "turnover_latest": self.turnover[-1] if self.turnover else None,
        }
