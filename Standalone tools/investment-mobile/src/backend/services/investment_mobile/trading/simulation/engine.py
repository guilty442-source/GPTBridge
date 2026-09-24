"""SimulationTradingEngine — paper/shadow facade.

Isolation contract: this engine owns ONLY paper ledgers (paper-* ids)
and never holds broker adapters, real account ids, or real order books.
Order acceptance requires mode==PAPER (ModeGate stage 'orders'); in
SHADOW only immutable signal records are produced. LIVE paths do not
exist here at all.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..backtest.cost import TradingCostEngine
from ..backtest.rules import capability_for, rules_for
from ..contracts import TradingMode
from ..fund.engine import MutualFundEngine
from ..market.calendar import TradingCalendar
from ..market.history import CandleStore
from ..modes import ModeGate
from .accounts import PaperAccountService
from .contracts import (
    PaperAccount, PaperExecution, PaperOrder, PaperOrderStatus,
    ShadowSignal, StrategyRun,
)
from .execution import PaperExecutionEngine
from .fund_settlement import PaperFundSettlementService
from .loop import PaperStrategyCoordinator, StrategyExecutionLoop
from .orders import PaperOrderManagementSystem
from .performance import (
    PaperPerformanceService, ShadowPaperComparison,
    StrategyBenchmarkService,
)
from .positions import PaperPositionService
from .recovery import SimulationRecoveryService
from .risk import PaperRiskEngine
from .shadow import ShadowTradingService, SignalOutcomeTracker

_CANON = {"tw": "TAIWAN_EQUITY", "us": "US_EQUITY",
          "fund": "MUTUAL_FUND"}
_SHORT = {"TAIWAN_EQUITY": "tw", "US_EQUITY": "us",
          "MUTUAL_FUND": "fund"}
_BROKER = {"TAIWAN_EQUITY": "CATHAY_SECURITIES",
           "US_EQUITY": "FUBON_SUBBROKERAGE",
           "MUTUAL_FUND": "MUTUAL_FUND_PROVIDER"}


def _canon(market: str) -> str:
    return _CANON.get(market, market)


def _short(market: str) -> str:
    return _SHORT.get(market, market)


class SimulationTradingEngine:
    def __init__(
        self, state_dir: Path, mode_gate: ModeGate,
        candle_store: CandleStore, calendar: TradingCalendar,
        fund_engine: MutualFundEngine,
        cost_engine: TradingCostEngine,
    ) -> None:
        self._dir = Path(state_dir) / "simulation"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._gate = mode_gate
        self._candles = candle_store
        self._calendar = calendar
        self._cost = cost_engine

        self.accounts = PaperAccountService(self._dir)
        self.positions = PaperPositionService(self._dir)
        self.orders = PaperOrderManagementSystem(self._dir)
        self.exec_engine = PaperExecutionEngine()
        self.risk = PaperRiskEngine(self._dir)
        self.shadow = ShadowTradingService(self._dir)
        self.outcomes = SignalOutcomeTracker(candle_store, fund_engine)
        self.coordinator = PaperStrategyCoordinator(
            self._dir, self.accounts, self.orders)
        self.recovery = SimulationRecoveryService(self._dir)
        self.fund_settlement = PaperFundSettlementService(
            self._dir, nav=fund_engine.nav, fees=fund_engine.fees,
            accounts=self.accounts, risk=self.risk,
            recovery=self.recovery)
        self.performance = PaperPerformanceService(
            self.accounts, self.positions, self.orders,
            fund_positions=self.fund_settlement.positions)
        self.benchmark = StrategyBenchmarkService(candle_store)
        self.shadow_paper = ShadowPaperComparison()
        self._loops: dict[str, StrategyExecutionLoop] = {}

    def close(self) -> None:
        for svc in (self.accounts, self.positions, self.orders,
                    self.risk, self.shadow, self.recovery):
            try:
                svc.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # paper accounts / cash
    # ------------------------------------------------------------------
    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        account = PaperAccount(
            account_id=str(payload.get("account_id") or ""),
            account_name=str(payload.get("account_name") or ""),
            market=str(payload.get("market") or ""),
            base_currency=str(payload.get("base_currency") or ""),
            initial_capital=Decimal(str(payload.get("initial_capital")
                                          or 0)))
        res = self.accounts.create(account)
        if res.get("ok"):
            self.recovery.record_event("paper_account",
                                       {"account_id": account.account_id})
        return res

    def deposit(self, account_id: str, amount) -> dict[str, Any]:
        return self.accounts.deposit(account_id, amount)

    def withdraw(self, account_id: str, amount) -> dict[str, Any]:
        return self.accounts.withdraw(account_id, amount)

    # ------------------------------------------------------------------
    # paper orders
    # ------------------------------------------------------------------
    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Full paper pipeline: mode → dedup → risk → reserve → order →
        fill. Never reaches broker adapters or formal ledgers."""
        if self._gate.mode is not TradingMode.PAPER:
            return {"ok": False, "error_code": "MODE_BLOCKED",
                    "mode": self._gate.mode.value,
                    "note": "PAPER 模擬下單僅在 PAPER 模式可用"}

        market = _canon(str(payload.get("market") or ""))
        account_id = str(payload.get("account_id") or "")
        account = self.accounts.get(account_id) if account_id else None
        if account is None:
            pa = self.accounts.for_market(market)
            account = pa.to_dict() if pa is not None else None
        if account is None:
            return {"ok": False, "error_code": "PAPER_ACCOUNT_NOT_FOUND"}
        account_id = account["account_id"]
        market = market or account["market"]

        # funds settle on the published-NAV cycle in a sim-only journal —
        # never through the equity candle-fill path
        if (market == "MUTUAL_FUND" or str(
                payload.get("instrument_id") or "").startswith("fund:")):
            return self.fund_settlement.submit(
                payload, account,
                strategy_run=self._run_for(
                    str(payload.get("strategy_id") or "")))

        order = PaperOrder(
            account_id=account_id,
            instrument_id=str(payload.get("instrument_id") or ""),
            side=str(payload.get("side") or ""),
            quantity=Decimal(str(payload.get("quantity") or 0)),
            order_type=str(payload.get("order_type") or "market"),
            strategy_id=str(payload.get("strategy_id") or ""),
            strategy_version=int(payload.get("strategy_version") or 0),
            limit_price=payload.get("limit_price"),
            reference_price=payload.get("reference_price"),
            currency=str(payload.get("currency")
                         or account["base_currency"]),
            expires_at=float(payload.get("expires_at") or 0),
            client_order_id=str(payload.get("client_order_id") or ""))

        # idempotency first — a repeated client_order_id returns the
        # existing order without touching cash or the book again
        if order.client_order_id:
            dup = self.orders.find_by_client(order.client_order_id)
            if dup is not None:
                return {"ok": True, "dedup": True, "order": dup}

        bars = self._candles.candles(order.instrument_id, "1d")
        latest = bars[-1] if bars else None
        quote_age = (None if latest is None else max(
            0.0, time.time() - latest.candle_end.timestamp()))
        ref = (order.limit_price or order.reference_price or
               (Decimal(str(latest.close)) if latest else Decimal("0")))

        cash = self.accounts.cash(account_id)
        equity = (Decimal(cash["available"]) + Decimal(cash["reserved"])
                  + Decimal(cash["unsettled"]))
        decision = self.risk.evaluate(
            account_id=account_id, instrument_id=order.instrument_id,
            side=order.side, quantity=order.quantity, price=ref,
            strategy_run=self._run_for(order.strategy_id),
            positions=self.positions.list(account_id),
            open_orders=[o.to_dict() for o in self.orders.open_orders()],
            cash_available=Decimal(cash["available"]),
            equity=equity, peak_equity=equity,
            daily_pnl=Decimal("0"), quote_age_s=quote_age)
        if decision["outcome"] != "ALLOW":
            self.orders.reject(order, "risk:" + decision["outcome"])
            return {"ok": False,
                    "error_code": "RISK_" + decision["outcome"],
                    "decision": decision, "order": order.to_dict()}

        notional = order.quantity * ref
        if order.side in ("buy", "subscribe"):
            fee_est = self._fee(order, notional, account)
            res = self.accounts.reserve(account_id, notional + fee_est,
                                        ref=order.order_id)
            if not res.get("ok"):
                self.orders.reject(order, "insufficient_cash")
                return {"ok": False, "error_code": "INSUFFICIENT_CASH",
                        "order": order.to_dict()}
        else:
            held = self.positions.get(account_id, order.instrument_id)
            if held is None or held.quantity < order.quantity:
                self.orders.reject(order, "insufficient_position")
                return {"ok": False,
                        "error_code": "INSUFFICIENT_POSITION",
                        "order": order.to_dict()}

        sub = self.orders.submit(order)
        if not sub.get("ok"):
            self.accounts.release_all(account_id, order.order_id)
            return sub

        seq = self.recovery.record_event("paper_order", {
            "order_id": order.order_id, "account_id": account_id})

        market_open = self._calendar.is_open(
            _short(market), datetime.now(timezone.utc))
        allow_eod = bool(payload.get("allow_eod_fill"))
        fill = self.exec_engine.try_fill(
            order, candle=latest,
            broker_id=str(payload.get("broker_id")
                          or _BROKER.get(market, "")),
            market=market, event_seq=seq,
            market_open=market_open or allow_eod)
        if fill.get("filled"):
            return self._fill(order, fill["execution"], account)
        if fill.get("ok") is False:
            # stale/missing data or unsupported type — release and reject
            self.accounts.release_all(account_id, order.order_id)
            self.orders.reject(
                order, str(fill.get("error_code") or "fill_failed"))
            return {"ok": False,
                    "error_code": fill.get("error_code", "FILL_FAILED"),
                    "order": order.to_dict()}
        self.recovery.record_event("paper_order_open",
                                   {"order_id": order.order_id,
                                    "reason": fill.get("reason")})
        return {"ok": True, "order": order.to_dict(),
                "fill": {k: v for k, v in fill.items()
                         if k != "execution"}}

    def _fill(self, order: PaperOrder, ex: PaperExecution,
              account: dict[str, Any]) -> dict[str, Any]:
        notional = ex.quantity * ex.price
        ex.fee = self._fee(order, notional, account)
        applied = self.orders.apply_fill(order.order_id, ex)
        if not applied.get("ok"):
            return applied
        self.positions.apply_fill(ex)
        acct_id = account["account_id"]
        if order.side in ("buy", "subscribe"):
            self.accounts.release_all(acct_id, order.order_id)
            self.accounts.post(acct_id, "buy_debit", -notional - ex.fee,
                               "AVAILABLE", ref=ex.exec_id)
        else:
            lag = rules_for(account["market"]).settlement_days
            self.accounts.post(
                acct_id, "sell_credit", notional - ex.fee, "UNSETTLED",
                ref=ex.exec_id,
                settle_on=(date.today() + timedelta(days=lag)).isoformat())
            self.accounts.post(acct_id, "fee", -ex.fee, "AVAILABLE",
                               ref=ex.exec_id)
        self.recovery.record_event("paper_fill", {
            "exec_id": ex.exec_id, "order_id": order.order_id})
        return {"ok": True, "order": applied["order"],
                "execution": ex.to_dict(), "simulated": True}

    def _fee(self, order: PaperOrder, notional: Decimal,
             account: dict[str, Any]) -> Decimal:
        res = self._cost.charge(
            notional=notional, direction=order.side,
            on_date=date.today().isoformat(),
            ctx={"market": account["market"],
                 "instrument_kind": "etf" if "ETF" in order.instrument_id
                 else "stock",
                 "broker_id": "CATHAY_SECURITIES"
                 if account["market"] == "TAIWAN_EQUITY"
                 else "FUBON_SUBBROKERAGE",
                 "account_id": account["account_id"]})
        return Decimal(str(res["total"]))

    # ------------------------------------------------------------------
    def cancel_order(self, order_id: str) -> dict[str, Any]:
        res = self.orders.cancel(order_id)
        if res.get("ok"):
            o = res["order"]
            self.accounts.release_all(o["account_id"], o["order_id"])
            self.recovery.record_event("paper_cancel",
                                       {"order_id": order_id})
        return res

    def expire_due(self) -> dict[str, Any]:
        res = self.orders.expire_due()
        for oid in res.get("expired", []):
            o = self.orders.get(oid)
            if o:
                self.accounts.release_all(o["account_id"], oid)
        res["fund_settlement"] = self.fund_settlement.advance()
        return res

    def process_market_event(self, instrument_id: str,
                             market: str = "") -> dict[str, Any]:
        """Retry open orders against the latest confirmed bar — queued
        (market_closed) and partially-filled orders pick up new data."""
        filled: list[dict[str, Any]] = []
        for o in list(self.orders.open_orders()):
            if o.instrument_id != instrument_id:
                continue
            account = self.accounts.get(o.account_id)
            if account is None:
                continue
            bars = self._candles.candles(o.instrument_id, "1d")
            fill = self.exec_engine.try_fill(
                o, candle=bars[-1] if bars else None,
                broker_id=_BROKER.get(_canon(account["market"]), ""),
                market=_canon(market or account["market"]))
            if fill.get("filled"):
                res = self._fill(o, fill["execution"], account)
                filled.append({"order_id": o.order_id,
                               "ok": res.get("ok")})
        self.expire_due()
        return {"ok": True, "filled": filled}

    def apply_corporate(self, account_id: str, instrument_id: str,
                        kind: str, ratio="1",
                        cash_amount="0") -> dict[str, Any]:
        """Controlled adjustment: split/stock dividend scale position;
        cash dividend / ETF distribution posts a dividend ledger entry."""
        res = self.positions.apply_corporate(
            account_id, instrument_id, kind, ratio=ratio)
        if not res.get("ok"):
            return res
        cash_amt = Decimal(str(cash_amount))
        if cash_amt > 0:
            qty = Decimal(res["position"]["quantity"])
            self.accounts.post(account_id, "dividend",
                               cash_amt * qty if kind != "cash_total"
                               else cash_amt,
                               "AVAILABLE", ref=f"corp:{instrument_id}")
        self.recovery.record_event("paper_corporate", {
            "account_id": account_id, "instrument_id": instrument_id,
            "kind": kind})
        return res

    # ------------------------------------------------------------------
    # shadow signals
    # ------------------------------------------------------------------
    def record_shadow_signal(self, payload: dict[str, Any]
                             ) -> dict[str, Any]:
        if not self._gate.allows("decisions"):
            return {"ok": False, "error_code": "MODE_BLOCKED",
                    "mode": self._gate.mode.value,
                    "note": "SHADOW 訊號需 SHADOW/PAPER 模式"}
        sig = ShadowSignal(
            instrument_id=str(payload.get("instrument_id") or ""),
            market=str(payload.get("market") or ""),
            side=str(payload.get("side") or ""),
            strategy_id=str(payload.get("strategy_id") or ""),
            strategy_version=int(payload.get("strategy_version") or 0),
            model_id=str(payload.get("model_id") or ""),
            model_version=str(payload.get("model_version") or ""),
            reference_price=payload.get("reference_price"),
            data_revision=str(payload.get("data_revision") or ""),
            evidence_refs=list(payload.get("evidence_refs") or []),
            risk_note=str(payload.get("risk_note") or ""),
            valid_until=str(payload.get("valid_until") or ""))
        res = self.shadow.record(sig)
        if res.get("ok"):
            self.recovery.record_event("shadow_signal",
                                       {"signal_id": sig.signal_id})
        return res

    def signal_outcome(self, signal_id: str) -> dict[str, Any]:
        sig = next((s for s in self.shadow.signals()
                    if s["signal_id"] == signal_id), None)
        if sig is None:
            return {"ok": False, "error_code": "SIGNAL_NOT_FOUND"}
        return self.outcomes.evaluate(sig)

    # ------------------------------------------------------------------
    # strategy loops
    # ------------------------------------------------------------------
    def start_loop(self, run: StrategyRun,
                   strategy: dict[str, Any]) -> dict[str, Any]:
        res = self.coordinator.start(run)
        if not res.get("ok"):
            return res
        loop = StrategyExecutionLoop(run, strategy, self._loop_tick)
        self._loops[run.run_id] = loop
        return {"ok": True, "run": run.to_dict(), "loop": "running"}

    def loop_control(self, run_id: str, action: str) -> dict[str, Any]:
        loop = self._loops.get(run_id)
        if loop is None:
            return {"ok": False, "error_code": "LOOP_NOT_FOUND"}
        fn = {"pause": loop.pause, "resume": loop.resume,
              "stop": loop.stop}.get(action)
        if fn is None:
            return {"ok": False, "error_code": "ACTION_UNKNOWN"}
        res = fn()
        self.coordinator.set_status(run_id, loop.status)
        return res

    def loop_status(self) -> dict[str, Any]:
        return {"ok": True, "loops": [{
            "run_id": l.run.run_id, "status": l.status,
            "tick_count": l.tick_count, "last_tick": l.last_tick}
            for l in self._loops.values()]}

    def tick(self, market_event: dict[str, Any]) -> dict[str, Any]:
        """Drive all running loops once — caller supplies cadence; the
        loop never busy-polls."""
        return {"ok": True, "ticks": {
            rid: loop.tick(market_event)
            for rid, loop in self._loops.items()}}

    def _loop_tick(self, loop: StrategyExecutionLoop,
                   event: dict[str, Any]) -> dict[str, Any]:
        """market event → signals → paper orders for the run's strategy."""
        from ..strategy.signals import generate
        s = loop.strategy
        scope = s.get("instrument_scope") or []
        iid = event.get("instrument_id") or (scope[0] if scope else "")
        bars = self._candles.candles(iid, "1d")
        if len(bars) < 3:
            return {"skipped": "insufficient_bars"}
        closes = [float(b.close) for b in bars]
        sigs = generate(s.get("strategy_type", ""), closes,
                        params=s.get("parameters") or {})
        latest_sigs = [x for x in sigs if x.bar == len(bars) - 1]
        placed = []
        for sig in latest_sigs:
            res = self.submit_order({
                "account_id": loop.run.paper_account_id,
                "instrument_id": iid, "side": sig.side,
                "quantity": (s.get("parameters") or {}).get(
                    "quantity", 1),
                "order_type": "market",
                "strategy_id": s.get("strategy_id", ""),
                "strategy_version": s.get("version", 0),
                "allow_eod_fill": True,
                "client_order_id":
                    f"{loop.run.run_id}:{iid}:{len(bars)}:{sig.side}"})
            placed.append({"side": sig.side, "ok": res.get("ok")})
        return {"signals": len(latest_sigs), "orders": placed}

    # ------------------------------------------------------------------
    def _run_for(self, strategy_id: str) -> dict[str, Any] | None:
        for r in self.coordinator.runs():
            if (r["strategy_id"] == strategy_id
                    and r["status"] == "running"):
                return r
        return None

    def status(self) -> dict[str, Any]:
        return {
            "ok": True, "mode": self._gate.mode.value,
            "accounts": self.accounts.list(),
            "open_orders": len(self.orders.open_orders()),
            "loops": self.loop_status()["loops"],
            "last_seq": self.recovery._seq,
            "simulated": True,
        }

    def recover(self) -> dict[str, Any]:
        return self.recovery.recover(
            [o.to_dict() for o in self.orders.open_orders()])
