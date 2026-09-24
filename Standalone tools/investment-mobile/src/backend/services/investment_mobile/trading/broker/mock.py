"""MockBrokerAdapter — controlled fake broker for acceptance tests.

All capability functions report SUPPORTED so the full live pipeline
(auth → risk → OMS → gateway → adapter → reports → reconciliation) can
be exercised end-to-end. Failure modes are programmable: report loss,
timeout, partial fills, cancel/fill races, disconnect.

This adapter never targets a real broker — it exists so LIVE-path logic
is testable while real dispatch stays phase-locked.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from .base import BrokerAdapter


class MockBrokerAdapter(BrokerAdapter):
    broker_id = "MOCK_BROKER"
    market = "mock"
    label = "Mock Broker (test-only)"

    _CAPABILITIES: dict[str, str] = {
        fn: "SUPPORTED" for fn in (
            "connect", "disconnect", "get_account", "get_balance",
            "get_positions", "get_orders", "get_executions",
            "place_order", "cancel_order", "modify_order")
    }

    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir)
        self.connected = False
        # programmable failure knobs
        self.drop_report = False        # ack order but report nothing
        self.timeout_on_place = False   # raise → SUBMISSION_UNKNOWN
        self.partial_fill_pct: Decimal | None = None
        self.reject_next: str = ""      # one-shot rejection reason
        self.orders: dict[str, dict[str, Any]] = {}
        self.executions: list[dict[str, Any]] = []
        self.balance: dict[str, Any] = {}
        self.positions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    @property
    def api_verified(self) -> bool:
        return True   # mock is intrinsically verified for tests

    def connect(self, **kwargs: Any) -> dict[str, Any]:
        self.connected = True
        return {"ok": True, "connected": True}

    def disconnect(self, **kwargs: Any) -> dict[str, Any]:
        self.connected = False
        return {"ok": True, "connected": False}

    def get_account(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "account_id": kwargs.get("account_id"),
                "status": "active"}

    def get_balance(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "balances": dict(self.balance)}

    def get_positions(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True,
                "positions": list(self.positions.values())}

    def get_orders(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "orders": [
            {**o, "broker_order_id": bid}
            for bid, o in self.orders.items()]}

    def get_executions(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "executions": list(self.executions)}

    # ------------------------------------------------------------------
    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        """Gateway-facing dict API (live domain)."""
        if self.timeout_on_place:
            raise TimeoutError("mock broker timeout")
        if self.reject_next:
            reason, self.reject_next = self.reject_next, ""
            return {"ok": False, "error_code": "BROKER_REJECTED",
                    "rejection": reason}
        bid = f"bk-{uuid.uuid4().hex[:10]}"
        qty = Decimal(str(kwargs.get("quantity") or "0"))
        order = {
            "internal_order_id": kwargs.get("internal_order_id", ""),
            "client_order_key": kwargs.get("client_order_key", ""),
            "instrument_id": kwargs.get("instrument_id", ""),
            "side": kwargs.get("side", ""),
            "quantity": str(qty),
            "filled_quantity": "0",
            "state": "ACKNOWLEDGED" if not self.drop_report
                     else "SENT_UNREPORTED",
            "at": time.time(),
        }
        self.orders[bid] = order
        return {"ok": True, "broker_order_id": bid,
                "state": order["state"], "ack": not self.drop_report}

    def fill(self, broker_order_id: str, quantity, price,
             partial: bool = False) -> dict[str, Any]:
        """Test hook — simulate a broker fill report."""
        order = self.orders.get(str(broker_order_id))
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        qty = Decimal(str(quantity))
        prev = Decimal(order["filled_quantity"])
        if partial and self.partial_fill_pct:
            qty = qty * self.partial_fill_pct
        new = prev + qty
        if new > Decimal(order["quantity"]):
            return {"ok": False, "error_code": "OVERFILL"}
        order["filled_quantity"] = str(new)
        order["state"] = "FILLED" if new >= Decimal(
            order["quantity"]) else "PARTIALLY_FILLED"
        ex = {
            "broker_order_id": str(broker_order_id),
            "instrument_id": order["instrument_id"],
            "side": order["side"],
            "quantity": str(qty), "price": str(price),
            "broker_execution_id": f"bx-{uuid.uuid4().hex[:10]}",
            "at": time.time(),
        }
        self.executions.append(ex)
        return {"ok": True, "execution": ex,
                "order_state": order["state"]}

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        bid = str(kwargs.get("broker_order_id") or "")
        order = self.orders.get(bid)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if order["state"] == "FILLED":
            return {"ok": False, "error_code": "ALREADY_FILLED"}
        order["state"] = "CANCELLED"
        return {"ok": True, "broker_order_id": bid, "state": "CANCELLED",
                "filled_quantity": order["filled_quantity"]}

    def modify_order(self, **kwargs: Any) -> dict[str, Any]:
        bid = str(kwargs.get("broker_order_id") or "")
        order = self.orders.get(bid)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if order["state"] in ("FILLED", "CANCELLED"):
            return {"ok": False, "error_code": "ORDER_TERMINAL"}
        if "quantity" in kwargs:
            order["quantity"] = str(kwargs["quantity"])
        return {"ok": True, "broker_order_id": bid}


# ---------------------------------------------------------------------------
# Offline broker simulators — market-specific mock environments.
# Same contract as the real adapters, every record tagged SIMULATED, and
# they write to their own in-memory ledgers only — never to real account
# tables. No network exists anywhere on these classes.
# ---------------------------------------------------------------------------
class _SimulatedMixin:
    """Tag every outbound record SIMULATED + market-rule validation."""

    market_label = "mock"
    fee_model: dict[str, Any] = {}
    session_model: dict[str, Any] = {}
    error_model: dict[str, str] = {}

    def _sim(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["simulated"] = True
        payload["environment"] = self.broker_id
        return payload

    def get_account(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim({"ok": True,
                          "account_id": kwargs.get("account_id"),
                          "status": "active",
                          "broker_confirmed": False})

    def get_balance(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim({"ok": True, "balances": dict(self.balance)})

    def get_positions(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim({"ok": True,
                          "positions": list(self.positions.values())})

    def get_orders(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim({"ok": True, "orders": [
            {**o, "broker_order_id": bid, "simulated": True}
            for bid, o in self.orders.items()]})

    def get_executions(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim({"ok": True, "executions": [
            {**e, "simulated": True} for e in self.executions]})

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        err = self._validate_order(kwargs)
        if err:
            return self._sim(err)
        r = super().place_order(**kwargs)
        return self._sim(r)

    def fill(self, broker_order_id: str, quantity, price,
             partial: bool = False) -> dict[str, Any]:
        r = super().fill(broker_order_id, quantity, price,
                         partial=partial)
        return self._sim(r)

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim(super().cancel_order(**kwargs))

    def modify_order(self, **kwargs: Any) -> dict[str, Any]:
        return self._sim(super().modify_order(**kwargs))

    def _validate_order(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        return None


class MockCathayTwAdapter(_SimulatedMixin, MockBrokerAdapter):
    """模擬國泰台股環境 — lot 1000 / odd-lot / ±10% / T+2 / TWD."""

    broker_id = "MOCK_CATHAY_TW"
    market = "tw"
    market_label = "模擬國泰台股"
    label = "Mock 國泰證券（台股，SIMULATED）"
    fee_model = {
        "commission_rate": "0.001425", "commission_min": "20",
        "sell_tax_rate": "0.003", "etf_sell_tax_rate": "0.001",
        "settlement": "T+2", "currency": "TWD", "simulated": True,
    }
    session_model = {"regular": {"open": "09:00", "close": "13:30",
                                 "tz": "Asia/Taipei"}, "simulated": True}
    error_model = {
        "INSUFFICIENT_FUNDS": "可用資金不足",
        "LOT_SIZE": "委託數量不符整股規則",
        "SESSION_CLOSED": "非交易時段",
    }

    def _validate_order(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        qty = Decimal(str(kwargs.get("quantity") or "0"))
        odd_lot = bool(kwargs.get("odd_lot"))
        if qty <= 0:
            return {"ok": False, "error_code": "INVALID_QUANTITY",
                    "simulated": True}
        if not odd_lot and qty % 1000 != 0:
            return {"ok": False, "error_code": "LOT_SIZE",
                    "rejection": "整股委託須為 1000 股倍數（或標示零股）",
                    "simulated": True}
        return None


class MockFubonSubBrokerageAdapter(_SimulatedMixin, MockBrokerAdapter):
    """模擬富邦美股複委託 — 整股 / 限價 / 無盤前盤後 / T+1 / USD."""

    broker_id = "MOCK_FUBON_US"
    market = "us"
    market_label = "模擬富邦美股複委託"
    label = "Mock 富邦複委託（美股，SIMULATED）"
    fee_model = {
        "commission_rate": "0.0025", "commission_min_usd": "15",
        "sec_fee_rate": "0.0000278", "settlement": "T+1",
        "currency": "USD", "simulated": True,
    }
    session_model = {"regular": {"open": "09:30", "close": "16:00",
                                 "tz": "America/New_York"},
                     "extended_hours": False, "simulated": True}
    error_model = {
        "INSUFFICIENT_FUNDS": "美元購買力不足",
        "FRACTIONAL_UNSUPPORTED": "不支援碎股",
        "MARKET_ORDER_UNSUPPORTED": "複委託僅支援限價單",
        "EXTENDED_HOURS_UNSUPPORTED": "不支援盤前盤後",
    }

    def _validate_order(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        qty = Decimal(str(kwargs.get("quantity") or "0"))
        if qty <= 0 or qty != int(qty):
            return {"ok": False, "error_code": "FRACTIONAL_UNSUPPORTED",
                    "rejection": "複委託不支援碎股", "simulated": True}
        if str(kwargs.get("order_type") or "limit") != "limit":
            return {"ok": False, "error_code": "MARKET_ORDER_UNSUPPORTED",
                    "rejection": "複委託僅支援限價單", "simulated": True}
        if kwargs.get("extended_hours"):
            return {"ok": False,
                    "error_code": "EXTENDED_HOURS_UNSUPPORTED",
                    "rejection": "複委託不支援盤前盤後", "simulated": True}
        return None
