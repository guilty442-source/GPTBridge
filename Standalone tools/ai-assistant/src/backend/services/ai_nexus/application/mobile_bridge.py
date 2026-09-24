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
        if operation == "record_order":
            order = payload.get("order")
            if not isinstance(order, dict):
                return {"ok": False, "error_code": "INVALID_ORDER"}
            self._store.record_order(order)
            return {"ok": True, "recorded": "order"}
        if operation == "record_fill":
            fill = payload.get("fill")
            if not isinstance(fill, dict):
                return {"ok": False, "error_code": "INVALID_FILL"}
            self._store.record_fill(fill)
            return {"ok": True, "recorded": "fill"}
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
