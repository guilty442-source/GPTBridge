"""BacktestEngine — single-market equity backtester with PIT gating.

Flow per bar t:
  1. signals generated from bars[0..t-1] (strategy sees only confirmed
     bars — publish lag applied via PIT slice)
  2. fills execute on bar t open (next_open) or close (configurable)
  3. costs charged via TradingCostEngine with scoped rules
  4. equity/cash/positions journal appended

Outputs the full metric set — never just total return. Every result is
`simulated=True` and carries assumptions + survivorship flag.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..intelligence.indicators import max_drawdown, volatility
from ..market.history import CandleStore
from ..strategy.contracts import StrategyDefinition
from ..strategy.signals import generate
from .contracts import (
    BacktestConfig, BacktestResult, BacktestTrade, EquityPoint,
)
from .cost import TradingCostEngine
from .execution import ExecutionSimulationEngine, FillResult
from .pit import slice_bars_at
from .rules import rules_for, capability_for
from .universe import HistoricalUniverseService


class BacktestEngine:
    def __init__(
        self, state_dir: Path, candle_store: CandleStore,
        cost_engine: TradingCostEngine | None = None,
        universe: HistoricalUniverseService | None = None,
        exec_engine: ExecutionSimulationEngine | None = None,
    ) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = candle_store
        self._cost = cost_engine or TradingCostEngine(self._dir)
        self._universe = universe or HistoricalUniverseService(self._dir)
        self._exec = exec_engine or ExecutionSimulationEngine()
        self._results_path = self._dir / "backtest_results.jsonl"

    # ------------------------------------------------------------------
    def run(self, config: BacktestConfig,
            strategy: StrategyDefinition) -> dict[str, Any]:
        errors = config.validate() + strategy.validate()
        if errors:
            return {"ok": False, "errors": errors}
        if config.market != strategy.market:
            return {"ok": False, "error_code": "MARKET_MISMATCH"}

        scope = config.instrument_scope
        survivorship = self._universe.survivorship_flag(config.market)
        warnings = ["SURVIVORSHIP_BIAS_RISK"] if survivorship else []

        cash = Decimal(str(config.initial_capital))
        positions: dict[str, Decimal] = {}
        trades: list[dict[str, Any]] = []
        positions_log: list[dict[str, Any]] = []
        equity_curve: list[EquityPoint] = []
        cost_total = Decimal("0")
        assumptions: list[str] = []

        market_rules = rules_for(config.market)
        cap = capability_for(str(config.assumptions.get("broker_id") or ""))
        publish_lag = timedelta(
            hours=float(config.assumptions.get("publish_lag_hours") or 0))
        adjustment = str(config.assumptions.get(
            "adjustment_type") or "adjusted")

        for iid in scope:
            bars = [b for b in self._store.candles(
                iid, config.timeframe, adjustment_type=adjustment)
                    if config.start_date <= b.candle_start.date()
                    <= config.end_date]
            if len(bars) < 2 and adjustment != "raw":
                # adjusted series absent → explicit fallback, never silent
                bars = [b for b in self._store.candles(
                    iid, config.timeframe, adjustment_type="raw")
                        if config.start_date <= b.candle_start.date()
                        <= config.end_date]
                if bars:
                    warnings.append("ADJUSTED_DATA_MISSING_RAW_USED")
                    assumptions.append("adjustment_fallback:raw")
            if len(bars) < 2:
                warnings.append(f"INSUFFICIENT_DATA:{iid}")
                continue
            closes = [float(b.close) for b in bars]
            highs = [float(b.high) for b in bars]
            lows = [float(b.low) for b in bars]
            volumes = [float(b.volume) for b in bars]

            signals = generate(strategy.strategy_type, closes,
                               volumes=volumes, highs=highs, lows=lows,
                               params=strategy.parameters)
            sig_bars = {s.bar: s for s in signals}

            for i, bar in enumerate(bars):
                as_of = bar.candle_end + publish_lag
                # PIT: a signal is actionable only when its bar is inside
                # the confirmed prefix — publish lag shifts it later.
                visible = slice_bars_at(bars[:i + 1], as_of, publish_lag)
                confirmed = len(visible) - 1
                if i in sig_bars and i <= confirmed:
                    sig = sig_bars[i]
                    # fills happen on NEXT bar open (never the signal bar)
                    fill_bar = i + 1
                    if fill_bar < len(bars):
                        fb = bars[fill_bar]
                        side = sig.side
                        if side == "rebalance":
                            positions_log.append({
                                "bar": i, "instrument_id": iid,
                                "event": "rebalance_marker"})
                        else:
                            order_type = (config.execution_model
                                          if config.execution_model in
                                          ("market", "limit") else "market")
                            if cap is not None and not cap.allows(order_type):
                                res = FillResult(
                                    False, "none", Decimal(0),
                                    Decimal(0), Decimal(0),
                                    reason="order_type_not_supported")
                            else:
                                res = self._exec.simulate(
                                    side=side,
                                    quantity=strategy.parameters.get(
                                        "quantity", 1),
                                    order_type=order_type,
                                    bar={"open": fb.open, "high": fb.high,
                                         "low": fb.low,
                                         "close": fb.close},
                                    limit_price=strategy.parameters.get(
                                        "limit_price"),
                                    volume=Decimal(str(fb.volume)),
                                )
                            assumptions.extend(res.assumptions)
                            if res.filled:
                                notional = res.quantity * res.price
                                cost = self._cost.charge(
                                    notional=notional,
                                    direction=side,
                                    on_date=fb.candle_start.date().isoformat(),
                                    ctx={
                                        "market": config.market,
                                        "instrument_kind": "etf"
                                        if "ETF" in iid else "stock",
                                        "broker_id": str(
                                            config.assumptions.get(
                                                "broker_id") or ""),
                                    })
                                fee = Decimal(str(cost["total"]))
                                cost_total += fee
                                if side in ("buy", "subscribe"):
                                    cash_needed = notional + fee
                                    if cash < cash_needed:
                                        res = type(res)(
                                            False, "none", Decimal(0),
                                            Decimal(0), Decimal(0),
                                            reason="insufficient_cash")
                                    else:
                                        cash -= cash_needed
                                        positions[iid] = (
                                            positions.get(iid, Decimal(0))
                                            + res.quantity)
                                else:
                                    held = positions.get(iid, Decimal(0))
                                    sellable = min(res.quantity, held)
                                    if sellable <= 0:
                                        res = type(res)(
                                            False, "none", Decimal(0),
                                            Decimal(0), Decimal(0),
                                            reason="insufficient_position")
                                    else:
                                        cash += sellable * res.price - fee
                                        positions[iid] = held - sellable
                                        if positions[iid] <= 0:
                                            positions.pop(iid, None)
                                if res.filled:
                                    trades.append(BacktestTrade(
                                        instrument_id=iid, side=side,
                                        quantity=res.quantity,
                                        price=res.price, fee=fee,
                                        slippage=res.slippage,
                                        signal_bar=i, fill_bar=fill_bar,
                                        fill_time=fb.candle_start.isoformat(),
                                        notional=res.quantity * res.price,
                                        currency=config.currency,
                                        order_type=config.execution_model,
                                        fill_kind=res.fill_kind,
                                        reason=sig.reason).to_dict())
                                    positions_log.append({
                                        "bar": fill_bar,
                                        "instrument_id": iid,
                                        "quantity": str(
                                            positions.get(iid,
                                                          Decimal(0))),
                                        "cash": str(cash)})
                                else:
                                    # cash/position-gated rejection stays
                                    # in the journal as an unfilled row
                                    trades.append(BacktestTrade(
                                        instrument_id=iid, side=side,
                                        quantity=Decimal(0),
                                        price=Decimal(0),
                                        fee=Decimal(0),
                                        slippage=Decimal(0),
                                        signal_bar=i, fill_bar=fill_bar,
                                        fill_time=fb.candle_start.isoformat(),
                                        notional=Decimal(0),
                                        currency=config.currency,
                                        order_type=config.execution_model,
                                        fill_kind=res.fill_kind,
                                        reason=res.reason).to_dict())
                            else:
                                trades.append(BacktestTrade(
                                    instrument_id=iid, side=side,
                                    quantity=Decimal(0), price=Decimal(0),
                                    fee=Decimal(0), slippage=Decimal(0),
                                    signal_bar=i, fill_bar=fill_bar,
                                    fill_time=fb.candle_start.isoformat(),
                                    notional=Decimal(0),
                                    currency=config.currency,
                                    order_type=config.execution_model,
                                    fill_kind=res.fill_kind,
                                    reason=sig.reason).to_dict())

                # equity mark at bar close (single-instrument scope)
                pos_val = positions.get(iid, Decimal(0)) * Decimal(
                    str(bar.close))
                equity_curve.append(EquityPoint(
                    t=bar.candle_start.date().isoformat(),
                    equity=cash + pos_val, cash=cash,
                    positions_value=pos_val))

        metrics = self._metrics(
            [Decimal(str(p.equity)) for p in equity_curve],
            trades, cost_total, Decimal(str(config.initial_capital)))
        result = BacktestResult(
            config={
                "strategy_id": config.strategy_id,
                "strategy_version": config.strategy_version,
                "market": config.market,
                "instrument_scope": scope,
                "start_date": str(config.start_date),
                "end_date": str(config.end_date),
                "initial_capital": str(config.initial_capital),
                "currency": config.currency,
                "execution_model": config.execution_model,
                "fee_model": config.fee_model,
                "slippage_model": config.slippage_model,
                "parameters": dict(strategy.parameters),
            },
            initial_capital=str(config.initial_capital),
            final_equity=str(equity_curve[-1].equity) if equity_curve
            else str(config.initial_capital),
            total_return=metrics["total_return"],
            annualized_return=metrics["annualized"],
            max_drawdown=metrics["max_drawdown"],
            volatility=metrics["volatility"],
            sharpe=metrics["sharpe"], sortino=metrics["sortino"],
            trade_count=metrics["trade_count"],
            win_rate=metrics["win_rate"], avg_win=metrics["avg_win"],
            avg_loss=metrics["avg_loss"],
            profit_factor=metrics["profit_factor"],
            cost_total=str(cost_total),
            equity_curve=[p.to_dict() for p in equity_curve],
            positions_log=positions_log,
            trades=trades, assumptions=sorted(set(assumptions)),
            warnings=warnings, data_revision=config.data_revision,
            survivorship_risk=survivorship, run_id=config.run_id)
        self._persist(result)
        return {"ok": True, "result": result.to_dict()}

    # ------------------------------------------------------------------
    def _metrics(self, equity: list[Decimal], trades: list[dict],
                 cost_total: Decimal, initial: Decimal) -> dict[str, Any]:
        if len(equity) < 2:
            return {k: "0" for k in ("total_return", "annualized",
                                     "max_drawdown", "volatility",
                                     "sharpe", "sortino", "win_rate",
                                     "avg_win", "avg_loss",
                                     "profit_factor")} | {
                "trade_count": 0}
        rets = [float(e2 / e1 - 1) for e1, e2 in zip(equity, equity[1:])
                if e1 > 0]
        total = float(equity[-1] / equity[0] - 1)
        days = max(1, len(equity))
        annual = (1 + total) ** (252 / days) - 1 if total > -1 else -1
        dd = abs(max_drawdown([float(e) for e in equity]) or 0)
        vol = volatility([float(e) for e in equity]) or 0
        mean = sum(rets) / len(rets) if rets else 0
        sharpe = (mean * 252) / vol if vol > 0 else 0
        downside = [r for r in rets if r < 0]
        dvol = (sum(r * r for r in downside) / len(downside)) ** 0.5 \
            if downside else 0
        sortino = (mean * 252) / (dvol * (252 ** 0.5)) if dvol > 0 else 0

        wins = [t for t in trades if t["side"] == "sell"
                and float(t["price"]) > 0]
        losses = []
        sell_pnls = []
        # approximate per-trade pnl vs first buy price
        buy_px: dict[str, Decimal] = {}
        for t in trades:
            if t["side"] in ("buy", "subscribe"):
                buy_px.setdefault(t["instrument_id"], Decimal(t["price"]))
            else:
                bp = buy_px.get(t["instrument_id"])
                if bp:
                    sell_pnls.append(
                        float(Decimal(t["price"]) / bp - 1))
        pos_pnls = [p for p in sell_pnls if p > 0]
        neg_pnls = [p for p in sell_pnls if p <= 0]
        win_rate = len(pos_pnls) / len(sell_pnls) if sell_pnls else 0
        avg_win = sum(pos_pnls) / len(pos_pnls) if pos_pnls else 0
        avg_loss = sum(neg_pnls) / len(neg_pnls) if neg_pnls else 0
        pf = (sum(pos_pnls) / abs(sum(neg_pnls))
              if neg_pnls else float("inf"))

        return {
            "total_return": f"{total:.6f}", "annualized": f"{annual:.6f}",
            "max_drawdown": f"{dd:.6f}", "volatility": f"{vol:.6f}",
            "sharpe": f"{sharpe:.4f}", "sortino": f"{sortino:.4f}",
            "trade_count": len([t for t in trades if t["fill_kind"] != "none"]),
            "win_rate": f"{win_rate:.4f}", "avg_win": f"{avg_win:.6f}",
            "avg_loss": f"{avg_loss:.6f}",
            "profit_factor": (f"{pf:.4f}" if pf != float("inf") else "inf"),
        }

    def _persist(self, result: BacktestResult) -> None:
        import json
        with self._results_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")

    def results(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self._results_path.exists():
            return []
        import json
        lines = self._results_path.read_text("utf-8").splitlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
