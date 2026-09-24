"""Investment-mobile bridge — governed envelope commands.

``xingcheng → ai-assistant`` carries only two fixed commands
(``authorize_investment_mobile_route``); every operation rides inside the
payload's ``operation`` field. The bridge is the write path by which the
investment-mobile engine cluster forwards authoritative records (signals,
orders, fills, mode changes, audit events) into this store.
"""

from __future__ import annotations

from typing import Any

from ..domain.contract import DOMAIN_AUTO_TRADING, DOMAIN_IDS
from ..infrastructure.trading_store import TradingStore


class InvestmentMobileBridge:
    """Submit-only bridge for the investment-mobile companion tool."""

    def __init__(self, store: TradingStore, ai_connections: Any) -> None:
        self._store = store
        self._ai = ai_connections

    # ------------------------------------------------------------------
    async def snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compact business snapshot for the companion tool."""
        return {
            "ok": True,
            "product": "星澄 AI 投資管理與自動操盤系統",
            "domains": list(DOMAIN_IDS),
            "trading_mode": self._store.kv_get(
                DOMAIN_AUTO_TRADING, "trading_mode", "ANALYSIS"
            ),
            "positions": self._store.positions(),
            "signal_count": len(self._store.signals(limit=10000)),
            "open_orders": [
                o for o in self._store.orders(limit=200)
                if o.get("status") in ("created", "submitted")
            ],
            "ai_connections": self._ai.status(),
        }

    async def submit_instruction(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation") or "").strip()
        if not operation:
            return {"ok": False, "error_code": "MISSING_OPERATION"}

        # Engine → business record forwarding (authoritative mirror writes).
        if operation == "record_signal":
            signal = payload.get("signal")
            if not isinstance(signal, dict):
                return {"ok": False, "error_code": "INVALID_SIGNAL"}
            self._store.record_signal(signal)
            return {"ok": True, "recorded": "signal"}
        if operation == "record_proposal":
            proposal = payload.get("proposal")
            if not isinstance(proposal, dict):
                return {"ok": False, "error_code": "INVALID_PROPOSAL"}
            self._store.record_proposal(proposal)
            return {"ok": True, "recorded": "proposal"}
        if operation == "record_decision":
            decision = payload.get("decision")
            if not isinstance(decision, dict):
                return {"ok": False, "error_code": "INVALID_DECISION"}
            self._store.record_decision(decision)
            return {"ok": True, "recorded": "decision"}
        if operation == "record_order":
            order = payload.get("order")
            if not isinstance(order, dict):
                return {"ok": False, "error_code": "INVALID_ORDER"}
            self._store.record_order(order)
            return {"ok": True, "recorded": "order"}
        if operation == "record_receipt":
            receipt = payload.get("receipt")
            if not isinstance(receipt, dict):
                return {"ok": False, "error_code": "INVALID_RECEIPT"}
            self._store.record_receipt(receipt)
            return {"ok": True, "recorded": "receipt"}
        if operation == "record_execution":
            execution = payload.get("execution")
            if not isinstance(execution, dict):
                return {"ok": False, "error_code": "INVALID_EXECUTION"}
            self._store.record_execution(execution)
            return {"ok": True, "recorded": "execution"}
        if operation == "record_audit":
            event = payload.get("event")
            if not isinstance(event, dict):
                return {"ok": False, "error_code": "INVALID_EVENT"}
            self._store.record_audit(event)
            return {"ok": True, "recorded": "audit"}
        if operation == "record_mode":
            mode = str(payload.get("mode") or "").upper()
            if mode not in ("ANALYSIS", "SHADOW", "PAPER", "LIVE"):
                return {"ok": False, "error_code": "MODE_UNKNOWN"}
            self._store.kv_set(DOMAIN_AUTO_TRADING, "trading_mode", mode)
            return {"ok": True, "recorded": "mode", "mode": mode}
        if operation == "record_authorization":
            grant = payload.get("grant")
            if not isinstance(grant, dict) or not grant.get("granted_by"):
                return {"ok": False, "error_code": "INVALID_GRANT"}
            self._store.record_authorization(grant)
            return {"ok": True, "recorded": "authorization"}

        # Market-data mirror writes from the engine (authoritative copy).
        if operation == "record_market_quote":
            quote = payload.get("quote")
            if not isinstance(quote, dict):
                return {"ok": False, "error_code": "INVALID_QUOTE"}
            self._store.record_market_quote(quote)
            return {"ok": True, "recorded": "market_quote"}
        if operation == "record_market_candle":
            candle = payload.get("candle")
            if not isinstance(candle, dict):
                return {"ok": False, "error_code": "INVALID_CANDLE"}
            self._store.record_market_candle(candle)
            return {"ok": True, "recorded": "market_candle"}
        if operation == "record_market_status":
            status = payload.get("status")
            if not isinstance(status, dict):
                return {"ok": False, "error_code": "INVALID_STATUS"}
            self._store.record_market_status(status)
            return {"ok": True, "recorded": "market_status"}

        # Fund domain mirror writes from the fund engine (authoritative copy).
        if operation == "record_fund_nav":
            nav = payload.get("nav")
            if not isinstance(nav, dict):
                return {"ok": False, "error_code": "INVALID_NAV"}
            self._store.record_fund_nav(nav)
            return {"ok": True, "recorded": "fund_nav"}
        if operation == "record_fund_transaction":
            txn = payload.get("transaction")
            if not isinstance(txn, dict):
                return {"ok": False, "error_code": "INVALID_TRANSACTION"}
            self._store.record_fund_transaction(txn)
            return {"ok": True, "recorded": "fund_transaction"}
        if operation == "record_fund_distribution":
            dist = payload.get("distribution")
            if not isinstance(dist, dict):
                return {"ok": False, "error_code": "INVALID_DISTRIBUTION"}
            self._store.record_fund_distribution(dist)
            return {"ok": True, "recorded": "fund_distribution"}
        if operation == "record_fund_recommendation":
            rec = payload.get("recommendation")
            if not isinstance(rec, dict):
                return {"ok": False, "error_code": "INVALID_RECOMMENDATION"}
            self._store.record_fund_recommendation(rec)
            return {"ok": True, "recorded": "fund_recommendation"}

        # AI-intelligence mirror writes (advisory records, never orders).
        if operation == "record_ai_recommendation":
            rec = payload.get("recommendation")
            if not isinstance(rec, dict):
                return {"ok": False, "error_code": "INVALID_RECOMMENDATION"}
            self._store.record_ai_recommendation(rec)
            return {"ok": True, "recorded": "ai_recommendation"}
        if operation == "record_analysis_run":
            run = payload.get("run")
            if not isinstance(run, dict):
                return {"ok": False, "error_code": "INVALID_RUN"}
            self._store.record_analysis_run(run)
            return {"ok": True, "recorded": "analysis_run"}
        if operation == "record_strategy":
            row = payload.get("strategy")
            if not isinstance(row, dict):
                return {"ok": False, "error_code": "INVALID_STRATEGY"}
            self._store.record_strategy(row)
            return {"ok": True, "recorded": "strategy"}
        if operation == "record_backtest_result":
            result = payload.get("result")
            if not isinstance(result, dict) or not result.get("simulated"):
                return {"ok": False,
                        "error_code": "INVALID_BACKTEST_RESULT"}
            self._store.record_backtest_result(result)
            return {"ok": True, "recorded": "backtest_result"}

        # Simulation-layer mirror (shadow signals + paper executions —
        # simulated flag enforced; never treated as real trading records).
        if operation == "record_shadow_signal":
            sig = payload.get("signal")
            if not isinstance(sig, dict) or not sig.get("simulated"):
                return {"ok": False,
                        "error_code": "INVALID_SHADOW_SIGNAL"}
            self._store.record_shadow_signal(sig)
            return {"ok": True, "recorded": "shadow_signal"}
        if operation == "record_paper_execution":
            ex = payload.get("execution")
            if not isinstance(ex, dict) or not ex.get("simulated"):
                return {"ok": False,
                        "error_code": "INVALID_PAPER_EXECUTION"}
            self._store.record_paper_execution(ex)
            return {"ok": True, "recorded": "paper_execution"}

        # Shared settings (companion-owned settings flow through here).
        if operation == "update_shared_settings":
            settings = payload.get("settings")
            if not isinstance(settings, dict):
                return {"ok": False, "error_code": "INVALID_SETTINGS"}
            for key, value in settings.items():
                self._store.kv_set("shared-settings", str(key), value)
            return {"ok": True, "recorded": "settings"}

        # Market research request → 星澄 through the AI channel.
        if operation in ("market_search", "analysis"):
            prompt = str(payload.get("instruction") or operation)
            result = await self._ai.consult(prompt, "investment-mobile")
            return {"ok": result.get("ok") is not False, "analysis": result}

        return {"ok": False, "error_code": "OPERATION_UNKNOWN", "operation": operation}
