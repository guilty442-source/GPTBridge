"""CATHAY_SECURITIES — 國泰證券台股 adapter (offline contract surface).

Scope: 台灣股票與 ETF. The official trading API is not yet integrated —
every function reports UNKNOWN until the real API surface is verified
against Cathay documentation (UNKNOWN is never treated as supported).
No UI-automation fallback: simulated mouse/keyboard submission is NOT a
trading API and is never used.

This module defines the *local contract* only: account/balance/
position/order/execution record shapes, the TW fee model and the broker
error model. There is intentionally NO transport implementation — every
live method returns BROKER_INTEGRATION_DISABLED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import BrokerAdapter


# ---------------------------------------------------------------------------
# Contract record shapes (local schema — real API payloads map onto these)
# ---------------------------------------------------------------------------
@dataclass
class CathayAccount:
    account_id: str
    holder_name: str = ""
    branch_code: str = ""
    currency: str = "TWD"
    status: str = "active"
    broker_confirmed: bool = False   # manual/offline data is never confirmed


@dataclass
class CathayBalance:
    account_id: str
    currency: str = "TWD"
    available: str = "0"
    reserved: str = "0"
    in_settlement: str = "0"         # T+2 proceeds not yet settled
    broker_confirmed: bool = False


@dataclass
class CathayPosition:
    account_id: str
    instrument_id: str               # tw:TW_STOCK|TW_ETF:<code>:TWD
    quantity: str = "0"
    avg_cost: str = "0"
    market: str = "tw"
    broker_confirmed: bool = False


@dataclass
class CathayOrder:
    broker_order_id: str
    client_order_key: str
    account_id: str
    instrument_id: str
    side: str                        # buy | sell
    order_type: str                  # market | limit
    quantity: str
    limit_price: str = ""
    filled_quantity: str = "0"
    status: str = "submitted"
    day_trade: bool = False
    odd_lot: bool = False


@dataclass
class CathayExecution:
    execution_id: str
    broker_order_id: str
    instrument_id: str
    quantity: str
    price: str
    fee: str = "0"
    tax: str = "0"
    traded_at: float = 0.0


# ---------------------------------------------------------------------------
# TW fee model (declared defaults — unverified, configurable per account)
# ---------------------------------------------------------------------------
CATHAY_FEE_MODEL: dict[str, Any] = {
    "commission_rate": "0.001425",       # 0.1425% statutory max
    "commission_discount": "1.0",        # e-brokerage discount, unverified
    "commission_min": "20",              # TWD minimum fee
    "sell_tax_rate": "0.003",            # 證交稅 0.3%
    "etf_sell_tax_rate": "0.001",        # ETF 證交稅 0.1%
    "day_trade_tax_rate": "0.0015",      # 當沖減半
    "settlement": "T+2",
    "currency": "TWD",
    "verified": False,
}

# Broker error model — canonical codes the adapter maps native errors to.
CATHAY_ERROR_MODEL: dict[str, str] = {
    "INSUFFICIENT_FUNDS": "可用資金不足",
    "INSUFFICIENT_POSITION": "庫存不足",
    "PRICE_LIMIT": "漲跌停限制",
    "LOT_SIZE": "委託數量不符整股/零股規則",
    "SESSION_CLOSED": "非交易時段",
    "ORDER_NOT_FOUND": "委託不存在",
    "DUPLICATE_CLIENT_KEY": "客戶端委託識別碼重複",
    "AUTHENTICATION_FAILED": "憑證或登入失效",
    "RATE_LIMITED": "API 流量限制",
    "BROKER_UNAVAILABLE": "券商系統暫時不可用",
    "UNKNOWN": "未分類錯誤",
}

# Regular session + odd-lot windows (declared, unverified).
CATHAY_SESSION_MODEL: dict[str, Any] = {
    "regular": {"open": "09:00", "close": "13:30", "tz": "Asia/Taipei"},
    "odd_lot_intraday": {"open": "09:00", "close": "13:30"},
    "odd_lot_afterhours": {"open": "13:40", "close": "14:30"},
    "verified": False,
}


class CathayTwAdapter(BrokerAdapter):
    broker_id = "CATHAY_SECURITIES"
    market = "tw"
    label = "國泰綜合證券（台股）"

    # Order types the Cathay TW account is expected to support once the
    # official API is verified — declared UNKNOWN until then.
    _CAPABILITIES: dict[str, str] = {
        "connect": "UNKNOWN",
        "disconnect": "UNKNOWN",
        "get_account": "UNKNOWN",
        "get_balance": "UNKNOWN",
        "get_positions": "UNKNOWN",
        "get_orders": "UNKNOWN",
        "get_executions": "UNKNOWN",
        "place_order": "UNKNOWN",
        "cancel_order": "UNKNOWN",
        "modify_order": "UNKNOWN",
    }

    declared_market_traits = {
        "lot_size": 1000, "odd_lot": True, "price_limit_pct": "0.10",
        "settlement": "T+2", "order_types_expected": ["market", "limit"],
        "currency": "TWD",
        "api_verified": False,
    }

    fee_model = CATHAY_FEE_MODEL
    error_model = CATHAY_ERROR_MODEL
    session_model = CATHAY_SESSION_MODEL

    # No transport attribute exists on purpose — offline phase.

    # ------------------------------------------------------------------
    def _disabled(self, fn: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "BROKER_INTEGRATION_DISABLED",
            "function": fn,
            "broker_id": self.broker_id,
            "note": "official API integration pending; contract surface "
                    "only — no network call was attempted",
        }

    def connect(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("connect")

    def disconnect(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("disconnect")

    def get_account(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("get_account")

    def get_balance(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("get_balance")

    def get_positions(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("get_positions")

    def get_orders(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("get_orders")

    def get_executions(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("get_executions")

    def place_order(self, order):  # OMS-only entry — stays closed
        from ..contracts import OrderReceipt, OrderStatus
        return OrderReceipt(
            order_id=order.order_id,
            status=OrderStatus.ADAPTER_DENIED.value,
            rejection="BROKER_INTEGRATION_DISABLED",
            simulated=False,
        )

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("cancel_order")

    def modify_order(self, **kwargs: Any) -> dict[str, Any]:
        return self._disabled("modify_order")
