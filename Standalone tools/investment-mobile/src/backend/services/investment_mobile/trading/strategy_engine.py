"""strategy domain — signal book + strategy evaluation.

星澄 emits ``TradingSignal`` objects (advisory only). The strategy
engine is the ONLY component allowed to convert signals into
``TradeProposal`` — proposals then enter the risk engine. AI can never
construct an OrderRequest or call a BrokerAdapter.

The C++ strategy engine (``native/strategy/strategy_engine.dll``)
provides the same ABI when built; the Python engine is the reference.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
from typing import Any

from .contracts import OrderSide, TradeProposal, TradingSignal

_NATIVE_DLL = (
    Path(__file__).resolve().parents[5] / "native" / "strategy" / "strategy_engine.dll"
)


class _NativeStrategy:
    """ctypes binding to the C++ strategy engine (evaluate_signal ABI)."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(str(_NATIVE_DLL))
        lib.strategy_evaluate_signal.restype = ctypes.c_int
        lib.strategy_evaluate_signal.argtypes = [
            ctypes.c_double, ctypes.c_double, ctypes.c_double,
            ctypes.c_double, ctypes.c_double,
            ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
        ]
        self._lib = lib

    def emit(
        self,
        confidence: float,
        quantity: float,
        price: float,
        min_confidence: float,
        max_quantity: float,
    ) -> tuple[float, float] | None:
        """(quantity, notional) when the signal qualifies, else None."""
        qty = ctypes.c_double(0.0)
        notional = ctypes.c_double(0.0)
        ok = self._lib.strategy_evaluate_signal(
            confidence, quantity, price, min_confidence, max_quantity,
            ctypes.byref(qty), ctypes.byref(notional),
        )
        if not ok:
            return None
        return float(qty.value), float(notional.value)


class StrategyEngine:
    """Signal intake + proposal generation.

    Strategies:
    - ``signal-follow``: converts qualifying 星澄 signals to proposals
      (confidence >= threshold, side/quantity sane).
    """

    def __init__(self, state_dir: Path) -> None:
        self._signals_path = state_dir / "signals.jsonl"
        self._native: _NativeStrategy | None = None
        if _NATIVE_DLL.exists():
            try:
                self._native = _NativeStrategy()
            except OSError:
                self._native = None
        self._min_confidence = 0.5

    @property
    def backend(self) -> str:
        return "native:strategy_engine" if self._native else "python"

    # ------------------------------------------------------------------
    def record_signal(self, payload: dict[str, Any]) -> dict[str, Any]:
        signal = TradingSignal(
            instrument_id=str(payload.get("instrument_id") or ""),
            market=str(payload.get("market") or ""),
            side=str(payload.get("side") or OrderSide.BUY.value),
            confidence=float(payload.get("confidence") or 0.0),
            price=payload.get("price"),
            quantity=float(payload.get("quantity") or 0.0),
            rationale=str(payload.get("rationale") or ""),
            source=str(payload.get("source") or "xingcheng"),
        )
        if not signal.instrument_id:
            return {"ok": False, "error_code": "INSTRUMENT_REQUIRED"}
        self._append_jsonl(self._signals_path, signal.to_dict())
        return {"ok": True, "signal_id": signal.signal_id, "recorded": "signal"}

    # ------------------------------------------------------------------
    def evaluate(self, signal_id: str | None = None) -> list[dict[str, Any]]:
        """Convert qualifying signals into TradeProposals (read-only)."""
        proposals: list[dict[str, Any]] = []
        for row in self._read_jsonl(self._signals_path):
            if signal_id and row.get("signal_id") != signal_id:
                continue
            confidence = float(row.get("confidence") or 0.0)
            quantity = float(row.get("quantity") or 0.0)
            price = float(row.get("price") or 0.0)
            if self._native is not None:
                emitted = self._native.emit(
                    confidence, quantity, price,
                    min_confidence=self._min_confidence,
                    max_quantity=0.0,  # the risk engine caps quantity
                )
                if emitted is None:
                    continue
                quantity = emitted[0]
            else:
                if confidence < self._min_confidence or quantity <= 0:
                    continue
            proposal = TradeProposal(
                instrument_id=row["instrument_id"],
                market=row["market"],
                side=row.get("side", OrderSide.BUY.value),
                quantity=quantity,
                price=row.get("price"),
                strategy_id="signal-follow",
                signal_id=row.get("signal_id", ""),
            )
            proposals.append(proposal.to_dict())
        return proposals

    # ------------------------------------------------------------------
    @staticmethod
    def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
