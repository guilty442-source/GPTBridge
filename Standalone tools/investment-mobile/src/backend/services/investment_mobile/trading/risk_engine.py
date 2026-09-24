"""risk domain — independent, fail-closed risk evaluation.

The engine evaluates ``TradeProposal`` objects ONLY. It reads its limits
from ``runtime/state/risk-limits.json`` (governed config) — proposal
``risk_params`` are informational and are never applied. 星澄/AI cannot
modify risk conditions: no command path accepts limit overrides.

When the C risk core (``native/risk/risk_core.dll``) is built, native
evaluation is used; the Python implementation is the reference/fallback
and parity-tested against the identical limit contract:

  market whitelist | quantity > 0 | price required | order notional |
  orders/day | daily loss | position notional | position weight |
  open orders | min cash buffer
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
from typing import Any

from .contracts import Position, RiskDecision, TradeProposal

_NATIVE_DLL = (
    Path(__file__).resolve().parents[5] / "native" / "risk" / "risk_core.dll"
)

_MARKET_BITS = {"tw": 1, "us": 2, "fund": 4}
_BUY_SIDES = {"buy", "subscribe"}


class _NativeLimits(ctypes.Structure):
    _fields_ = [
        ("max_order_notional", ctypes.c_double),
        ("max_position_notional", ctypes.c_double),
        ("max_daily_loss", ctypes.c_double),
        ("max_orders_per_day", ctypes.c_int),
        ("max_single_position_weight", ctypes.c_double),
        ("require_price", ctypes.c_int),
        ("allowed_market_mask", ctypes.c_uint),
        ("max_open_orders", ctypes.c_int),
        ("min_cash_buffer", ctypes.c_double),
    ]


class _NativeOrderInput(ctypes.Structure):
    _fields_ = [
        ("market_bit", ctypes.c_uint),
        ("side", ctypes.c_int),
        ("quantity", ctypes.c_double),
        ("notional", ctypes.c_double),
        ("existing_position_notional", ctypes.c_double),
        ("existing_position_value", ctypes.c_double),
        ("total_portfolio_value", ctypes.c_double),
        ("daily_order_count", ctypes.c_int),
        ("daily_realized_pnl", ctypes.c_double),
        ("open_order_count", ctypes.c_int),
        ("cash_after", ctypes.c_double),
    ]


class _NativeRiskCore:
    """ctypes binding to the C risk core — same decision contract."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(str(_NATIVE_DLL))
        lib.risk_evaluate_order.restype = ctypes.c_int
        lib.risk_evaluate_order.argtypes = [
            ctypes.POINTER(_NativeLimits),
            ctypes.POINTER(_NativeOrderInput),
        ]
        lib.risk_reason_name.restype = ctypes.c_char_p
        lib.risk_reason_name.argtypes = [ctypes.c_int]
        self._lib = lib

    def evaluate(
        self, limits: dict[str, Any], order: dict[str, Any]
    ) -> RiskDecision:
        mask = 0
        for market in limits.get("market_whitelist") or ():
            mask |= _MARKET_BITS.get(str(market), 0)
        native_limits = _NativeLimits(
            max_order_notional=float(limits.get("max_order_notional") or 0),
            max_position_notional=float(limits.get("max_position_notional") or 0),
            max_daily_loss=float(limits.get("max_daily_loss") or 0),
            max_orders_per_day=int(limits.get("max_orders_per_day") or 0),
            max_single_position_weight=float(
                limits.get("max_single_position_weight") or 0
            ),
            require_price=1 if limits.get("require_price", True) else 0,
            allowed_market_mask=mask,
            max_open_orders=int(limits.get("max_open_orders") or 0),
            min_cash_buffer=float(limits.get("min_cash_buffer") or 0),
        )
        native_order = _NativeOrderInput(
            market_bit=_MARKET_BITS.get(str(order["market"]), 0),
            side=1 if order["side"] in _BUY_SIDES else -1,
            quantity=float(order["quantity"]),
            notional=float(order["notional"]),
            existing_position_notional=float(order["existing_position_notional"]),
            existing_position_value=float(order["existing_position_value"]),
            total_portfolio_value=float(order["total_portfolio_value"]),
            daily_order_count=int(order["daily_order_count"]),
            daily_realized_pnl=float(order["daily_realized_pnl"]),
            open_order_count=int(order["open_order_count"]),
            cash_after=float(order["cash_after"]),
        )
        code = self._lib.risk_evaluate_order(
            ctypes.byref(native_limits), ctypes.byref(native_order)
        )
        reason = self._lib.risk_reason_name(code).decode("utf-8", "replace")
        return RiskDecision(
            approved=code == 0,
            reasons=[] if code == 0 else [reason],
            backend="native",
        )


class RiskEngine:
    def __init__(self, state_dir: Path) -> None:
        self._limits_path = state_dir / "risk-limits.json"
        self._limits: dict[str, Any] = self._load_limits()
        self._native: _NativeRiskCore | None = None
        if _NATIVE_DLL.exists():
            try:
                self._native = _NativeRiskCore()
            except OSError:
                self._native = None

    @property
    def backend(self) -> str:
        return "native:risk_core" if self._native else "python"

    @property
    def configured(self) -> bool:
        return bool(self._limits.get("max_order_notional"))

    def _load_limits(self) -> dict[str, Any]:
        defaults = {
            "max_order_notional": 1_000_000.0,
            "max_position_notional": 5_000_000.0,
            "max_daily_loss": 100_000.0,
            "max_orders_per_day": 1_000,
            "max_single_position_weight": 0.6,
            "require_price": True,
            "max_open_orders": 20,
            "min_cash_buffer": 10_000.0,
            "market_whitelist": ["tw", "us", "fund"],
        }
        try:
            user = json.loads(self._limits_path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                defaults.update(user)
        except Exception:
            pass
        return defaults

    # ------------------------------------------------------------------
    def evaluate(
        self,
        proposal: TradeProposal,
        positions: list[Position],
        open_orders: int,
        daily_pnl: float,
        cash_available: float,
        portfolio_value: float,
        daily_order_count: int = 0,
    ) -> RiskDecision:
        notional = proposal.effective_notional()
        is_buy = proposal.side in _BUY_SIDES
        target = next(
            (p for p in positions if p.instrument_id == proposal.instrument_id),
            None,
        )
        existing_notional = target.notional if target else 0.0
        existing_value = target.market_value if target else 0.0
        cash_after = cash_available - notional if is_buy else cash_available + notional
        order = {
            "market": proposal.market,
            "side": proposal.side,
            "quantity": proposal.quantity,
            "notional": notional,
            "existing_position_notional": existing_notional,
            "existing_position_value": existing_value,
            "total_portfolio_value": portfolio_value,
            "daily_order_count": daily_order_count,
            "daily_realized_pnl": daily_pnl,
            "open_order_count": open_orders,
            "cash_after": cash_after,
        }

        if self._native is not None:
            decision = self._native.evaluate(self._limits, order)
            decision.limits_checked = self._checked()
            return decision

        reasons = self._reference_reasons(order)
        return RiskDecision(
            approved=not reasons,
            reasons=reasons,
            limits_checked=self._checked(),
            backend="python",
        )

    # ------------------------------------------------------------------
    def _reference_reasons(self, order: dict[str, Any]) -> list[str]:
        """Python reference — identical limit contract to the C core."""
        limits = self._limits
        reasons: list[str] = []
        if order["market"] not in limits["market_whitelist"]:
            reasons.append("market_not_whitelisted")
        if order["quantity"] <= 0:
            reasons.append("quantity_invalid")
        if limits.get("require_price", True) and order["notional"] <= 0:
            reasons.append("no_price")
        if float(limits["max_order_notional"]) <= 0:
            reasons.append("order_notional_exceeds_limit")
        elif order["notional"] > float(limits["max_order_notional"]):
            reasons.append("order_notional_exceeds_limit")
        if int(limits["max_orders_per_day"]) <= 0 or (
            order["daily_order_count"] >= int(limits["max_orders_per_day"])
        ):
            reasons.append("daily_order_limit")
        if float(limits["max_daily_loss"]) <= 0 or (
            order["daily_realized_pnl"] <= -float(limits["max_daily_loss"])
        ):
            reasons.append("daily_loss_limit_breached")
        if float(limits["max_position_notional"]) <= 0:
            reasons.append("position_notional_exceeds_limit")
        else:
            projected = order["existing_position_notional"] + (
                order["notional"] * (1 if order["side"] in _BUY_SIDES else -1)
            )
            if projected > float(limits["max_position_notional"]):
                reasons.append("position_notional_exceeds_limit")
        weight = float(limits["max_single_position_weight"])
        if weight > 0 and order["total_portfolio_value"] > 0:
            projected_value = order["existing_position_value"] + (
                order["notional"] * (1 if order["side"] in _BUY_SIDES else -1)
            )
            if projected_value / order["total_portfolio_value"] > weight:
                reasons.append("position_weight_exceeds_limit")
        max_open = int(limits["max_open_orders"])
        if max_open > 0 and order["open_order_count"] >= max_open:
            reasons.append("too_many_open_orders")
        if order["cash_after"] < float(limits["min_cash_buffer"]):
            reasons.append("cash_below_minimum_buffer")
        return reasons

    def _checked(self) -> list[str]:
        return [
            "market_whitelist",
            "quantity",
            "require_price",
            "max_order_notional",
            "max_orders_per_day",
            "max_daily_loss",
            "max_position_notional",
            "max_single_position_weight",
            "max_open_orders",
            "min_cash_buffer",
        ]
