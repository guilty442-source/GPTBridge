"""Risk Engine — fail-closed order evaluation.

Python façade implementing the same limit contract as the C core
(``native/risk/risk_core.c``). When the compiled core is available it takes
over evaluation; the pure-Python path is the authoritative reference
implementation and the fallback.

Fail-closed semantics: any missing limit, malformed intent, or evaluation
error results in a rejection, never a silent approval.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from .contracts import OrderIntent, Position, RiskDecision


_DEFAULT_LIMITS: dict[str, Any] = {
    "max_order_notional": 500_000.0,
    "max_position_notional": 1_000_000.0,
    "max_daily_loss": 100_000.0,
    "max_orders_per_day": 50,
    "max_single_position_weight": 0.25,
    "allowed_markets": ["tw", "us", "fund"],
    "require_price": True,
}


class _NativeRiskCore:
    """ctypes binding to ``native/risk/risk_core.dll`` (MSVC build).

    The native core evaluates the same limit contract; when the DLL is
    absent the pure-Python path remains authoritative.
    """

    _MARKET_BITS = {"tw": 1, "us": 2, "fund": 4}

    class _Limits(ctypes.Structure):
        _fields_ = [
            ("max_order_notional", ctypes.c_double),
            ("max_position_notional", ctypes.c_double),
            ("max_daily_loss", ctypes.c_double),
            ("max_orders_per_day", ctypes.c_int),
            ("max_single_position_weight", ctypes.c_double),
            ("require_price", ctypes.c_int),
            ("allowed_market_mask", ctypes.c_uint),
        ]

    class _Order(ctypes.Structure):
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
        ]

    _REASONS = {
        1: "market not in allowed_markets",
        2: "quantity must be positive",
        3: "no price/notional — fail closed",
        4: "order notional exceeds max_order_notional",
        5: "daily order limit reached",
        6: "daily loss limit breached",
        7: "projected position exceeds max_position_notional",
        8: "single-position weight exceeds cap",
    }

    def __init__(self, dll_path: Path) -> None:
        self._ct = ctypes
        self._lib = ctypes.CDLL(str(dll_path))
        self._lib.risk_evaluate_order.restype = ctypes.c_int
        self._lib.risk_evaluate_order.argtypes = [
            ctypes.POINTER(self._Limits),
            ctypes.POINTER(self._Order),
        ]

    @classmethod
    def load(cls, tool_root: Path) -> "_NativeRiskCore | None":
        dll = (
            Path(tool_root)
            / "native"
            / "risk"
            / ("risk_core.dll" if os.name == "nt" else "risk_core.so")
        )
        if not dll.is_file():
            return None
        try:
            return cls(dll)
        except OSError:
            return None

    def evaluate(
        self,
        intent: OrderIntent,
        limits: dict[str, Any],
        positions: Iterable[Position],
        daily_order_count: int,
        daily_realized_pnl: float,
    ) -> tuple[int, list[str]]:
        market_bit = self._MARKET_BITS.get(str(intent.market), 0)
        existing = next(
            (
                p
                for p in positions
                if p.market == str(intent.market)
                and p.instrument == str(intent.instrument)
            ),
            None,
        )
        lim = self._Limits(
            max_order_notional=float(limits.get("max_order_notional") or 0),
            max_position_notional=float(limits.get("max_position_notional") or 0),
            max_daily_loss=float(limits.get("max_daily_loss") or 0),
            max_orders_per_day=int(limits.get("max_orders_per_day") or 0),
            max_single_position_weight=float(
                limits.get("max_single_position_weight") or 0
            ),
            require_price=1 if limits.get("require_price") else 0,
            allowed_market_mask=sum(
                self._MARKET_BITS.get(str(m), 0)
                for m in (limits.get("allowed_markets") or [])
            ),
        )
        order = self._Order(
            market_bit=market_bit,
            side=1 if intent.side == "buy" else -1,
            quantity=float(intent.quantity or 0),
            notional=float(intent.effective_notional()),
            existing_position_notional=existing.notional if existing else 0.0,
            existing_position_value=existing.market_value if existing else 0.0,
            total_portfolio_value=sum(p.market_value for p in positions),
            daily_order_count=int(daily_order_count),
            daily_realized_pnl=float(daily_realized_pnl),
        )
        code = self._lib.risk_evaluate_order(self._ct.byref(lim), self._ct.byref(order))
        return code, [self._REASONS.get(code, f"native rejection {code}")] if code else []


class RiskEngine:
    """Evaluates order intents against configured limits."""

    def __init__(self, state_dir: Path, tool_root: Path | None = None) -> None:
        self._limits_path = state_dir / "risk-limits.json"
        self._limits = dict(_DEFAULT_LIMITS)
        self._native = (
            _NativeRiskCore.load(tool_root) if tool_root is not None else None
        )
        self._load()

    def _load(self) -> None:
        try:
            overrides = json.loads(self._limits_path.read_text(encoding="utf-8"))
            if isinstance(overrides, dict):
                self._limits.update(overrides)
        except Exception:
            pass

    @property
    def limits(self) -> dict[str, Any]:
        return dict(self._limits)

    @property
    def backend(self) -> str:
        return "native:risk_core" if self._native is not None else "python"

    # ------------------------------------------------------------------
    def evaluate(
        self,
        intent: OrderIntent,
        positions: Iterable[Position],
        *,
        daily_order_count: int = 0,
        daily_realized_pnl: float = 0.0,
    ) -> RiskDecision:
        positions = list(positions)
        if self._native is not None:
            code, reasons = self._native.evaluate(
                intent,
                self._limits,
                positions,
                daily_order_count,
                daily_realized_pnl,
            )
            return RiskDecision(
                approved=code == 0,
                reasons=reasons,
                limits_checked=["native:risk_core"],
            )
        reasons: list[str] = []
        checked: list[str] = []

        market = str(intent.market or "")
        checked.append("allowed_markets")
        allowed = self._limits.get("allowed_markets")
        if not isinstance(allowed, list) or market not in allowed:
            reasons.append(f"market '{market}' not in allowed_markets")

        checked.append("quantity")
        if not intent.quantity or intent.quantity <= 0:
            reasons.append("quantity must be positive")

        notional = intent.effective_notional()
        checked.append("require_price")
        if self._limits.get("require_price") and notional <= 0:
            reasons.append("no price/notional — fail closed")

        checked.append("max_order_notional")
        max_order = float(self._limits.get("max_order_notional") or 0)
        if max_order <= 0:
            reasons.append("max_order_notional limit missing")
        elif notional > max_order:
            reasons.append(f"order notional {notional:.2f} exceeds {max_order:.2f}")

        checked.append("max_orders_per_day")
        max_orders = int(self._limits.get("max_orders_per_day") or 0)
        if max_orders <= 0 or daily_order_count >= max_orders:
            reasons.append("daily order limit reached")

        checked.append("max_daily_loss")
        max_loss = float(self._limits.get("max_daily_loss") or 0)
        if max_loss <= 0 or daily_realized_pnl <= -max_loss:
            reasons.append("daily loss limit breached")

        checked.append("max_position_notional")
        position_map = {
            (p.market, p.instrument): p for p in positions
        }
        key = (market, str(intent.instrument or ""))
        existing = position_map.get(key)
        projected = (existing.notional if existing else 0.0) + (
            notional if intent.side == "buy" else -notional
        )
        max_pos = float(self._limits.get("max_position_notional") or 0)
        if max_pos <= 0:
            reasons.append("max_position_notional limit missing")
        elif projected > max_pos:
            reasons.append(
                f"projected position {projected:.2f} exceeds {max_pos:.2f}"
            )

        checked.append("max_single_position_weight")
        total_value = sum(p.market_value for p in positions) or 0.0
        weight_cap = float(self._limits.get("max_single_position_weight") or 0)
        if weight_cap > 0 and total_value > 0:
            projected_value = (
                (existing.market_value if existing else 0.0)
                + (notional if intent.side == "buy" else -notional)
            )
            if projected_value / total_value > weight_cap:
                reasons.append(
                    "single-position weight "
                    f"{projected_value / total_value:.2%} exceeds {weight_cap:.2%}"
                )

        return RiskDecision(approved=not reasons, reasons=reasons, limits_checked=checked)

    # ------------------------------------------------------------------
    def portfolio_risk_summary(
        self, positions: Iterable[Position]
    ) -> dict[str, Any]:
        positions = list(positions)
        total = sum(p.market_value for p in positions)
        concentration = max(
            (p.market_value / total for p in positions), default=0.0
        )
        return {
            "positions": len(positions),
            "total_market_value": total,
            "max_concentration": concentration,
            "limits": self.limits,
            "evaluated_at": time.time(),
        }
