"""Deterministic signal generators — one per StrategyType.

Each generator takes a price/volume series (+ optional context) and
emits SignalEvent lists with explicit bar indices — the same function
is used by live evaluation and backtest replay so paths never diverge.
No LLM involvement: AI proposes parameters, never generates signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..intelligence.indicators import ema, rsi, sma
from .contracts import StrategyType


@dataclass
class SignalEvent:
    bar: int                    # index into the series (signal *seen* at bar)
    side: str                   # buy|sell|subscribe|redeem
    reason: str
    weight: float = 1.0         # fraction of scope budget (default full)


def generate(
    strategy_type: str,
    closes: Sequence[float],
    *,
    volumes: Sequence[float] | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    params: dict[str, Any] | None = None,
) -> list[SignalEvent]:
    """Dispatch to the type's generator. Unknown → empty (fail-closed)."""
    p = dict(params or {})
    if strategy_type == StrategyType.TREND_FOLLOWING:
        return _trend(closes, p)
    if strategy_type == StrategyType.MEAN_REVERSION:
        return _mean_reversion(closes, p)
    if strategy_type == StrategyType.MOMENTUM:
        return _momentum(closes, p)
    if strategy_type == StrategyType.BREAKOUT:
        return _breakout(closes, highs or closes, lows or closes, p)
    if strategy_type == StrategyType.FUND_RECURRING_INVESTMENT:
        return _recurring(closes, p)
    if strategy_type in (StrategyType.MULTI_FACTOR,
                         StrategyType.ASSET_ALLOCATION,
                         StrategyType.PORTFOLIO_REBALANCING):
        return _allocation(closes, p)
    return []


# ----------------------------------------------------------------------
def _trend(closes: Sequence[float], p: dict) -> list[SignalEvent]:
    """SMA crossover: fast above slow → buy; below → sell."""
    fast_n = int(p.get("fast", 5))
    slow_n = int(p.get("slow", 20))
    out: list[SignalEvent] = []
    prev: int | None = None
    for i in range(len(closes)):
        f = sma(closes[: i + 1], fast_n)
        s = sma(closes[: i + 1], slow_n)
        if not f or not s:
            continue
        state = 1 if f[-1] > s[-1] else -1
        if prev is not None and state != prev:
            out.append(SignalEvent(
                bar=i, side="buy" if state > 0 else "sell",
                reason=f"ma_cross:{fast_n}/{slow_n}"))
        prev = state
    return out


def _mean_reversion(closes: Sequence[float], p: dict) -> list[SignalEvent]:
    """RSI extremes: <30 buy, >70 sell."""
    lo = float(p.get("rsi_buy", 30))
    hi = float(p.get("rsi_sell", 70))
    out: list[SignalEvent] = []
    for i in range(15, len(closes)):
        r = rsi(closes[: i + 1], 14)
        if r is None:
            continue
        if r < lo:
            out.append(SignalEvent(bar=i, side="buy",
                                   reason=f"rsi<{lo}"))
        elif r > hi:
            out.append(SignalEvent(bar=i, side="sell",
                                   reason=f"rsi>{hi}"))
    return out


def _momentum(closes: Sequence[float], p: dict) -> list[SignalEvent]:
    """N-day momentum threshold."""
    n = int(p.get("lookback", 20))
    th = float(p.get("threshold", 0.05))
    out: list[SignalEvent] = []
    for i in range(n, len(closes)):
        mom = closes[i] / closes[i - n] - 1.0
        if mom > th:
            out.append(SignalEvent(bar=i, side="buy", reason="momentum>th"))
        elif mom < -th:
            out.append(SignalEvent(bar=i, side="sell", reason="momentum<-th"))
    return out


def _breakout(closes: Sequence[float], highs: Sequence[float],
              lows: Sequence[float], p: dict) -> list[SignalEvent]:
    """Donchian-channel breakout."""
    n = int(p.get("channel", 20))
    out: list[SignalEvent] = []
    for i in range(n, len(closes)):
        hh = max(highs[i - n:i])
        ll = min(lows[i - n:i])
        if closes[i] > hh:
            out.append(SignalEvent(bar=i, side="buy", reason="channel_up"))
        elif closes[i] < ll:
            out.append(SignalEvent(bar=i, side="sell", reason="channel_dn"))
    return out


def _recurring(closes: Sequence[float], p: dict) -> list[SignalEvent]:
    """Fund 定期定額: subscribe every N bars."""
    interval = int(p.get("interval_bars", 21))
    out = [SignalEvent(bar=i, side="subscribe", reason="recurring")
           for i in range(0, len(closes), interval)]
    return out


def _allocation(closes: Sequence[float], p: dict) -> list[SignalEvent]:
    """Allocation/rebalance skeleton: periodic rebalance marker."""
    interval = int(p.get("rebalance_bars", 63))
    return [SignalEvent(bar=i, side="rebalance", reason="periodic")
            for i in range(interval, len(closes), interval)]
