"""AutoTradingCoordinator — the unified pipeline.

    MarketEvent → DataValidation → StrategyEvaluation → TradingSignal
    → AIAnalysis (per policy) → TradeProposal → RiskDecision
    → PaperOrder → PaperExecution → PaperPortfolio → PerformanceAnalysis

SHADOW mode stops at signal recording — no fills, no cash movement.
PAPER mode goes through SimulationTradingEngine (MockBroker fill model,
client_order_id idempotency, PaperRiskEngine decision, cash reserve).

AIAnalysis is NOT a synchronous hard requirement for every strategy:
DETERMINISTIC strategies run without it; AI_ASSISTED strategies reuse
still-valid validated analyses and block when evidence is stale/missing.
"""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from ..strategy.signals import generate as generate_signals
from .runtime import RuntimeState


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class AutoTradingCoordinator:
    def __init__(self, *, manager: Any, dispatcher: Any,
                 allocator: Any, resources: Any, sessions: Any,
                 scheduler: Any, gate: Any, sim: Any,
                 ai_integration: Any, events: Any,
                 risk_monitor: Any | None = None) -> None:
        self.manager = manager
        self.dispatcher = dispatcher
        self.allocator = allocator
        self.resources = resources
        self.sessions = sessions
        self.scheduler = scheduler
        self.gate = gate                   # MonitoringDataGate
        self.sim = sim                     # SimulationTradingEngine
        self.ai = ai_integration           # AISignalIntegrationService
        self.events = events               # monitoring event store
        self.risk_monitor = risk_monitor

    # ------------------------------------------------------------------
    async def run_cycle(self, run_id: str,
                        event: dict[str, Any]) -> dict[str, Any]:
        """One pipeline pass for one strategy runtime. Every stage is
        recorded in the trace; failure at any stage is fail-closed."""
        run = self.manager.get(run_id)
        trace: list[dict[str, Any]] = []
        if run is None:
            return {"ok": False, "error_code": "RUN_NOT_FOUND"}
        state = run["state"]
        if state != RuntimeState.RUNNING:
            return {"ok": False, "error_code": "NOT_RUNNING",
                    "state": state}
        market = run["market"]
        iid = event.get("instrument_id") or ""
        scope = run.get("instrument_scope") or []
        if scope and iid and iid not in scope:
            return {"ok": True, "skipped": "out_of_scope",
                    "instrument_id": iid}
        trace.append({"stage": "MarketEvent",
                      "event_id": event.get("event_id")})

        # ---- DataValidation -------------------------------------------
        g = self.gate.check_instrument(iid, market)
        trace.append({"stage": "DataValidation", **g})
        if g["status"] in ("STALE", "UNAVAILABLE", "INCOMPLETE"):
            self.manager.transition(
                run_id, RuntimeState.DATA_BLOCKED,
                actor="system", reason=f"data {g['status']}")
            return {"ok": False, "error_code": "DATA_BLOCKED",
                    "gate": g, "trace": trace}

        # ---- Session gate ---------------------------------------------
        sess = self.sessions.allow_order(run, instrument_id=iid,
                                         side="buy")
        trace.append({"stage": "SessionCheck", **sess})

        # ---- StrategyEvaluation → TradingSignal -----------------------
        bars = self.sim._candles.candles(iid, "1d")
        closes = [float(b.close) for b in bars]
        vols = [float(b.volume) for b in bars]
        signals = generate_signals(
            str(run.get("strategy_type") or
                run.get("parameters", {}).get("type") or "MOMENTUM"),
            closes, volumes=vols,
            params=run.get("parameters") or {})
        latest = [s for s in signals if s.bar == len(closes) - 1]
        trace.append({"stage": "StrategyEvaluation",
                      "signals": len(latest)})
        if not latest:
            return {"ok": True, "trace": trace, "signals": 0}
        sig = latest[-1]
        signal = {
            "signal_id": f"sig-{run['run_id']}-{iid}-"
                         f"{int(event.get('timestamp') or time.time())}",
            "run_id": run_id,
            "strategy_id": run["strategy_id"],
            "strategy_version": run["strategy_version"],
            "instrument_id": iid, "market": market,
            "side": sig.side, "reason": sig.reason,
            "weight": sig.weight,
            "source_timestamp": event.get("timestamp"),
            "data_revision": event.get("data_revision"),
            "data_vintage": event.get("timestamp"),
            "simulated": True,
        }
        trace.append({"stage": "TradingSignal", **{
            "side": sig.side, "reason": sig.reason}})

        # ---- AIAnalysis (per policy) ----------------------------------
        if run["ai_policy"] == "AI_ASSISTED":
            conf = await self.ai.confirm(run, signal)
            trace.append({"stage": "AIAnalysis", **conf})
            if not conf.get("ok"):
                self.manager.transition(
                    run_id, RuntimeState.MODEL_BLOCKED,
                    actor="system",
                    reason=conf.get("error_code", "ai"))
                return {"ok": False,
                        "error_code": conf.get("error_code",
                                               "MODEL_BLOCKED"),
                        "trace": trace}
        else:
            trace.append({"stage": "AIAnalysis",
                          "skipped": "DETERMINISTIC policy"})

        # ---- TradeProposal + resource coordination --------------------
        price = Decimal(str(closes[-1]))
        account_id = run.get("account_id") or ""
        cash = self.sim.accounts.cash(account_id)
        qty = Decimal("0")
        if run["execution_mode"] == "PAPER":
            # capital gating is a PAPER concern — SHADOW signals carry
            # no cash commitment and never need an allocation plan
            budget = self.allocator.available(account_id,
                                              run["strategy_id"],
                                              cash=cash)
            if not budget.get("ok"):
                return {"ok": False,
                        "error_code": budget["error_code"],
                        "trace": trace}
            notional = _d(budget["budget"]) * _d(sig.weight or 1)
            qty = (notional / price).quantize(
                Decimal("1"), rounding=ROUND_DOWN) \
                if price else Decimal("0")
            if qty <= 0:
                return {"ok": True, "trace": trace,
                        "skipped": "zero_quantity"}
        proposal = {"run_id": run_id,
                    "strategy_id": run["strategy_id"],
                    "instrument_id": iid, "side": sig.side,
                    "quantity": str(qty), "reference_price": str(price),
                    "notional": str(qty * price), "signal": signal,
                    "simulated": True}
        res = self.resources.request(
            strategy_id=run["strategy_id"], run_id=run_id,
            instrument_id=iid, side=sig.side, quantity=qty,
            notional=qty * price,
            signal_key=f"{iid}|{sig.side}|"
                       f"{event.get('fingerprint', '')}")
        trace.append({"stage": "TradeProposal",
                      "reservation": res.get("reservation", {}),
                      "conflict": res.get("conflict", False)})
        if not res.get("ok"):
            return {"ok": False, "error_code": res["error_code"],
                    "trace": trace}

        # ---- SHADOW: record signal, never fill -------------------------
        if run["execution_mode"] == "SHADOW":
            rec = self.sim.shadow.record(_shadow_signal(signal, price))
            trace.append({"stage": "ShadowSignal",
                          "signal_id": signal["signal_id"]})
            return {"ok": True, "mode": "SHADOW", "signal": signal,
                    "shadow": rec, "trace": trace,
                    "note": "SHADOW 只記錄訊號——無成交、無資金變動"}

        # ---- PAPER: RiskDecision → PaperOrder → fill -------------------
        cap = self.allocator.check_order(
            account_id, run["strategy_id"], instrument_id=iid,
            notional=qty * price, cash=cash,
            strategy_exposure=_d(0), instrument_exposure=_d(0))
        if not cap.get("ok"):
            self.resources.release(res["reservation"]["reservation_id"])
            return {"ok": False, "error_code": cap["error_code"],
                    "trace": trace}
        if not sess.get("ok"):
            self.resources.release(
                res["reservation"]["reservation_id"])
            return {"ok": False, "error_code": sess["error_code"],
                    "trace": trace}
        order = self.sim.submit_order({
            "account_id": account_id, "instrument_id": iid,
            "market": market, "side": sig.side,
            "quantity": str(qty), "order_type": "market",
            "strategy_id": run["strategy_id"],
            "strategy_version": run["strategy_version"],
            "reference_price": str(price),
            "client_order_id":
                f"{run_id}|{iid}|{event.get('fingerprint','')}",
            "allow_eod_fill": True})
        trace.append({"stage": "PaperOrder",
                      "order_ok": order.get("ok"),
                      "error": order.get("error_code")})
        if not order.get("ok"):
            self.resources.release(
                res["reservation"]["reservation_id"])
            return {"ok": False, "error_code": order.get("error_code"),
                    "decision": order.get("decision"), "trace": trace}
        trace.append({"stage": "PaperExecution",
                      "status": order["order"].get("status")})
        return {"ok": True, "mode": "PAPER", "signal": signal,
                "order": order["order"], "fill": order.get("fill"),
                "trace": trace}


def _shadow_signal(signal: dict[str, Any], price: Decimal):
    from ..simulation.contracts import ShadowSignal
    return ShadowSignal(
        strategy_id=signal["strategy_id"],
        strategy_version=signal["strategy_version"],
        instrument_id=signal["instrument_id"],
        market=signal["market"],
        side=str(signal["side"]).upper(),
        reference_price=price,
        risk_note=signal["reason"],
        evidence_refs=[f"rev:{signal.get('data_revision') or ''}",
                       f"ts:{signal.get('source_timestamp') or ''}"],
        data_revision=str(signal.get("data_revision") or ""))
