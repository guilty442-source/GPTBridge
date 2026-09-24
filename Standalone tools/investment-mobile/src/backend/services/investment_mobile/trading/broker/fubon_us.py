"""FUBON_SUBBROKERAGE — 富邦證券美股複委託 adapter (offline contract).

Scope: 美國股票與 ETF via 富邦複委託. This is NOT the Fubon TW-stock
API — a separate account model, product capability set, trading session,
fee model, and order-type surface. Anything not yet confirmed against
the official sub-brokerage API is UNKNOWN (never treated as supported).

Declared (verified-by-rule, not by API) constraints: no extended-hours,
no short selling, no margin, no fractional shares.

Local contract only — account/balance/position/order/execution shapes,
the sub-brokerage fee model and the error model. NO transport exists;
every live method returns BROKER_INTEGRATION_DISABLED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import BrokerAdapter


# ---------------------------------------------------------------------------
# Contract record shapes (sub-brokerage account, USD books)
# ---------------------------------------------------------------------------
@dataclass
class FubonSubAccount:
    account_id: str
    holder_name: str = ""
    base_currency: str = "USD"
    funding_currency: str = "TWD"     # sub-brokerage settles via TWD→USD
    status: str = "active"
    broker_confirmed: bool = False


@dataclass
class FubonSubBalance:
    account_id: str
    currency: str = "USD"
    available: str = "0"
    reserved: str = "0"
    in_settlement: str = "0"          # T+1 proceeds not yet settled
    twd_settlement_pending: str = "0" # FX leg awaiting TWD settlement
    broker_confirmed: bool = False


@dataclass
class FubonSubPosition:
    account_id: str
    instrument_id: str                # us:US_STOCK|US_ETF:<symbol>:USD
    quantity: str = "0"               # whole shares only — no fractional
    avg_cost: str = "0"
    market: str = "us"
    broker_confirmed: bool = False


@dataclass
class FubonSubOrder:
    broker_order_id: str
    client_order_key: str
    account_id: str
    instrument_id: str
    side: str                         # buy | sell (no short)
    order_type: str                   # limit only at most sub-brokers
    quantity: str
    limit_price: str = ""
    filled_quantity: str = "0"
    status: str = "submitted"
    valid_for_session: str = "regular"  # no extended hours


@dataclass
class FubonSubExecution:
    execution_id: str
    broker_order_id: str
    instrument_id: str
    quantity: str
    price: str
    currency: str = "USD"
    fee_usd: str = "0"
    sec_fee: str = "0"                # SEC fee on sells
    traded_at: float = 0.0


# ---------------------------------------------------------------------------
# Sub-brokerage fee model (declared defaults — unverified)
# ---------------------------------------------------------------------------
FUBON_SUB_FEE_MODEL: dict[str, Any] = {
    "commission_rate": "0.0025",          # ~0.25% typical sub-brokerage
    "commission_min_usd": "15",           # typical minimum ticket fee
    "sec_fee_rate": "0.0000278",          # SEC fee on sell notional
    "taf_fee_per_share": "0.000166",      # FINRA TAF (sells)
    "fx_spread_note": "TWD↔USD conversion spread charged by broker",
    "settlement": "T+1",
    "currency": "USD",
    "verified": False,
}

FUBON_SUB_ERROR_MODEL: dict[str, str] = {
    "INSUFFICIENT_FUNDS": "美元購買力不足",
    "INSUFFICIENT_POSITION": "庫存不足",
    "MARKET_ORDER_UNSUPPORTED": "複委託可能不支援市價單",
    "FRACTIONAL_UNSUPPORTED": "不支援零股/碎股",
    "EXTENDED_HOURS_UNSUPPORTED": "不支援盤前盤後交易",
    "SESSION_CLOSED": "非交易時段",
    "ORDER_NOT_FOUND": "委託不存在",
    "DUPLICATE_CLIENT_KEY": "客戶端委託識別碼重複",
    "AUTHENTICATION_FAILED": "憑證或登入失效",
    "RATE_LIMITED": "API 流量限制",
    "BROKER_UNAVAILABLE": "券商系統暫時不可用",
    "UNKNOWN": "未分類錯誤",
}

FUBON_SUB_SESSION_MODEL: dict[str, Any] = {
    "regular": {"open": "09:30", "close": "16:00", "tz": "America/New_York"},
    "extended_hours": False,
    "half_day_close": "13:00",
    "verified": False,
}


class FubonUsAdapter(BrokerAdapter):
    broker_id = "FUBON_SUBBROKERAGE"
    market = "us"
    label = "富邦證券複委託（美股）"

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
        "lot_size": 1, "odd_lot": False, "fractional": False,
        "extended_hours": False, "short_selling": False,
        "margin": False, "settlement": "T+1", "currency": "USD",
        "api_verified": False,
        "note": "sub-brokerage — Fubon TW-stock API is a different "
                "interface and must never be used for US orders",
    }

    fee_model = FUBON_SUB_FEE_MODEL
    error_model = FUBON_SUB_ERROR_MODEL
    session_model = FUBON_SUB_SESSION_MODEL

    # No transport attribute exists on purpose — offline phase.

    # ------------------------------------------------------------------
    def _disabled(self, fn: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "BROKER_INTEGRATION_DISABLED",
            "function": fn,
            "broker_id": self.broker_id,
            "note": "official sub-brokerage API integration pending; "
                    "contract surface only — no network call attempted",
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

    def place_order(self, order):
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
