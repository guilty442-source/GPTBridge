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

from ...domain.contract import DOMAIN_AUTO_TRADING
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
                "fills": store.fills(limit=int(payload.get("limit") or 100)),
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
        raise PermissionError("PERMISSION_DENIED")
