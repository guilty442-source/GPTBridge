"""AI 自動操盤 domain — business-side trading coordination.

The engines (strategy/risk/OMS/broker adapters) live in the
investment-mobile tool boundary and decide execution. This domain keeps
the authoritative business mirror: signal book, order/fill records,
authorization grants and the mirrored trading mode.

Boundary: investment-mobile is a submit-only channel participant — it is
never a route target, so the business layer records what the engine
forwards; it does not command the engine.
"""

from __future__ import annotations

from ...domain.contract import DOMAIN_AUTO_TRADING, DOMAIN_SIM_TRADING
from .base import BusinessDomain


class AutoTradingDomain(BusinessDomain):
    domain_id = DOMAIN_AUTO_TRADING
    label = "AI 自動操盤"
    commands = frozenset(
        {
            "investment_trade_signals",
            "investment_trade_orders",
            "investment_trade_mode",
            "investment_trade_authorizations",
            "investment_trade_audit",
            "investment_strategy_list",
            "investment_backtest_results",
            "investment_autotrade_control",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_trade_signals":
            return {
                "ok": True,
                "domain": self.domain_id,
                "signals": store.signals(limit=int(payload.get("limit") or 100)),
            }
        if command == "investment_trade_orders":
            return {
                "ok": True,
                "domain": self.domain_id,
                "orders": store.orders(limit=int(payload.get("limit") or 100)),
                "executions": store.executions(limit=int(payload.get("limit") or 100)),
            }
        if command == "investment_trade_mode":
            return {
                "ok": True,
                "domain": self.domain_id,
                # Mirrored from the engine via governed audit forwarding;
                # the authoritative gate lives in investment-mobile.
                "mode": store.kv_get(self.domain_id, "trading_mode", "ANALYSIS"),
                "modes": ["ANALYSIS", "SHADOW", "PAPER", "LIVE"],
                "live_requires": "explicit-human-authorization+verified-broker-api",
            }
        if command == "investment_trade_authorizations":
            return {
                "ok": True,
                "domain": self.domain_id,
                "authorizations": store.authorizations(),
            }
        if command == "investment_trade_audit":
            return {
                "ok": True,
                "domain": self.domain_id,
                "events": store.audit_tail(int(payload.get("limit") or 50)),
            }
        if command == "investment_strategy_list":
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "strategies": store.strategies(),
            }
        if command == "investment_backtest_results":
            sid = str(payload.get("strategy_id") or "") or None
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "results": store.backtest_results(strategy_id=sid),
            }
        if command == "investment_autotrade_control":
            # investment-mobile is a submit-only participant — there is
            # no governed route into it, so UI controls cannot reach the
            # engine. Record the intent for audit and answer honestly.
            store.record_audit({
                "type": "autotrade_control_intent",
                "action": str(payload.get("action") or ""),
                "run_id": str(payload.get("run_id") or ""),
                "actor": "ui-user",
                "result": "CONTROL_CHANNEL_UNAVAILABLE",
            })
            return {
                "ok": False,
                "error_code": "CONTROL_CHANNEL_UNAVAILABLE",
                "note": "投資管家引擎為 submit-only 通道端點——"
                        "前端控制指令尚無授權路由送達；"
                        "操作意圖已記入審計，待通道核准後生效。",
                "integration": "incomplete",
            }
        raise PermissionError("PERMISSION_DENIED")


class SimTradingDomain(BusinessDomain):
    """AI 模擬操盤 console — read-only mirror of SHADOW/PAPER records.

    Same boundary as AutoTradingDomain: investment-mobile is submit-only;
    the console displays mirrored simulation state and never commands the
    engine. Controls live on the investment-mobile command surface.
    """

    domain_id = DOMAIN_SIM_TRADING
    label = "AI 模擬操盤"
    commands = frozenset(
        {
            "investment_sim_status",
            "investment_sim_signals",
            "investment_sim_executions",
            "investment_sim_performance",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_sim_status":
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "label": "PAPER / SHADOW 模擬區",
                "mode": store.kv_get(self.domain_id, "trading_mode",
                                     "ANALYSIS"),
                "live_requires":
                    "explicit-human-authorization+verified-broker-api",
            }
        if command == "investment_sim_signals":
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "signals": store.shadow_signals(
                    limit=int(payload.get("limit") or 200)),
            }
        if command == "investment_sim_executions":
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "executions": store.paper_executions(
                    limit=int(payload.get("limit") or 200)),
            }
        if command == "investment_sim_performance":
            # PAPER performance rows mirror only — never real performance
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "label": "PAPER PERFORMANCE",
                "executions": store.paper_executions(
                    limit=int(payload.get("limit") or 500)),
            }
        raise PermissionError("PERMISSION_DENIED")
