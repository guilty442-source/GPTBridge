"""risk domain — independent, fail-closed risk evaluation.

The engine evaluates ``TradeProposal`` objects ONLY. It reads its limits
from ``runtime/state/risk-limits.json`` (governed config) — proposal
``risk_params`` are informational and are never applied. 星澄/AI cannot
modify risk conditions: no command path accepts limit overrides.

When the C risk core (``native/risk/risk_core.dll``) is built, native
evaluation is used; the Python implementation is the reference/fallback
and parity-tested.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
from typing import Any

from .contracts import Position, RiskDecision, TradeProposal

_NATIVE_DLL = (
    Path(__file__).resolve().parents[4] / "native" / "risk" / "risk_core.dll"
)


class _NativeLimits(ctypes.Structure):
    _fields_ = [
        ("max_order_notional", ctypes.c_double),
        ("max_position_notional", ctypes.c_double),
        ("max_market_exposure_pct", ctypes.c_double),
        ("max_daily_loss", ctypes.c_double),
        ("max_open_orders", ctypes.c_int),
        ("max_quantity", ctypes.c_double),
        ("min_cash_buffer", ctypes.c_double),
    ]


class _NativeRiskCore:
    """ctypes binding to the C risk core — same decision semantics."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(str(_NATIVE_DLL))
        lib.risk_evaluate.restype = ctypes.c_int
        lib.risk_evaluate.argtypes = [
            ctypes.POINTER(_NativeLimits),
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        self._lib = lib

    def evaluate(
        self, limits: dict[str, float], args: tuple[float, ...]
    ) -> RiskDecision:
        native = _NativeLimits(
            max_order_notional=float(limits.get("max_order_notional") or 0),
            max_position_notional=float(limits.get("max_position_notional") or 0),
            max_market_exposure_pct=float(limits.get("max_market_exposure_pct") or 0),
            max_daily_loss=float(limits.get("max_daily_loss") or 0),
            max_open_orders=int(limits.get("max_open_orders") or 0),
            max_quantity=float(limits.get("max_quantity") or 0),
            min_cash_buffer=float(limits.get("min_cash_buffer") or 0),
        )
        buf = ctypes.create_string_buffer(512)
        ok = self._lib.risk_evaluate(
            ctypes.byref(native), *args, buf, ctypes.sizeof(buf)
        )
        reasons = [
            r for r in buf.value.decode("utf-8", "replace").split(";") if r
        ]
        return RiskDecision(approved=bool(ok), reasons=reasons, backend="native")


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
            "max_market_exposure_pct": 60.0,
            "max_daily_loss": 100_000.0,
            "max_open_orders": 20,
            "max_quantity": 100_000.0,
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
    ) -> RiskDecision:
        notional = proposal.effective_notional()
        target = next(
            (p for p in positions if p.instrument_id == proposal.instrument_id),
            None,
        )
        position_after = (target.notional if target else 0.0) + notional
        market_notional = sum(
            p.notional for p in positions if p.market == proposal.market
        ) + notional
        exposure_pct = (
            (market_notional / portfolio_value * 100.0)
            if portfolio_value > 0
            else (100.0 if market_notional > 0 else 0.0)
        )
        cash_after = cash_available - (
            notional if proposal.side in ("buy", "subscribe") else -notional
        )
        args = (
            notional,
            position_after,
            exposure_pct,
            daily_pnl,
            cash_after,
            open_orders,
        )

        if self._native is not None:
            decision = self._native.evaluate(self._limits, args)
            decision.limits_checked = self._checked()
            return decision

        reasons: list[str] = []
        if proposal.market not in self._limits["market_whitelist"]:
            reasons.append("market_not_whitelisted")
        if notional > float(self._limits["max_order_notional"]):
            reasons.append("order_notional_exceeds_limit")
        if position_after > float(self._limits["max_position_notional"]):
            reasons.append("position_notional_exceeds_limit")
        if exposure_pct > float(self._limits["max_market_exposure_pct"]):
            reasons.append("market_exposure_exceeds_limit")
        if daily_pnl < -float(self._limits["max_daily_loss"]):
            reasons.append("daily_loss_limit_breached")
        if open_orders >= int(self._limits["max_open_orders"]):
            reasons.append("too_many_open_orders")
        if proposal.quantity > float(self._limits["max_quantity"]):
            reasons.append("quantity_exceeds_limit")
        if cash_after < float(self._limits["min_cash_buffer"]):
            reasons.append("cash_below_minimum_buffer")
        return RiskDecision(
            approved=not reasons,
            reasons=reasons,
            limits_checked=self._checked(),
            backend="python",
        )

    def _checked(self) -> list[str]:
        return [
            "market_whitelist",
            "max_order_notional",
            "max_position_notional",
            "max_market_exposure_pct",
            "max_daily_loss",
            "max_open_orders",
            "max_quantity",
            "min_cash_buffer",
        ]
