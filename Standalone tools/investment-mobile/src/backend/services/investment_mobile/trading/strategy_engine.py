"""Strategy Engine — turns 星澄 analysis signals into formal order intents.

The Python façade hosts strategy registration, signal bookkeeping and the
backtest/research hooks (model training, backtesting per the language
division of labour). Formal model inference is delegated to the C++
component (``native/strategy/strategy_engine.cpp``) when it is loaded;
the declarative rule path is the reference implementation.

A signal never reaches the OMS unless an authorized strategy converts it
into an :class:`OrderIntent` — 星澄 produces advice, the strategy engine
produces proposals, the risk engine and OMS decide.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .contracts import OrderIntent, Signal


class StrategyEngine:
    """Registered strategies + signal book."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._signals_path = state_dir / "signal-book.json"
        self._signals: list[dict[str, Any]] = []
        self._strategies: dict[str, Callable[[Signal], OrderIntent | None]] = {}
        self._load()
        self.register("signal-follow", self._signal_follow)

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._signals_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._signals = data[-500:]
        except Exception:
            self._signals = []

    def _persist(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._signals_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._signals[-500:], ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._signals_path)

    # ------------------------------------------------------------------
    def register(
        self, strategy_id: str, evaluator: Callable[[Signal], OrderIntent | None]
    ) -> None:
        self._strategies[str(strategy_id)] = evaluator

    def strategies(self) -> list[str]:
        return sorted(self._strategies)

    # Reference strategy: follow the signal as proposed (subject to risk
    # gate downstream). Returns None when the signal is not actionable.
    def _signal_follow(self, signal: Signal) -> OrderIntent | None:
        if signal.confidence < 0.5 or signal.quantity <= 0:
            return None
        return OrderIntent(
            instrument=signal.instrument,
            market=signal.market,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.price,
            strategy_id="signal-follow",
            signal_id=signal.signal_id,
        )

    # ------------------------------------------------------------------
    def ingest_signal(self, signal: Signal) -> dict[str, Any]:
        self._signals.append(signal.to_dict())
        self._persist()
        intents = [
            intent
            for evaluator in self._strategies.values()
            if (intent := evaluator(signal)) is not None
        ]
        return {
            "signal": signal.to_dict(),
            "intents": [i.to_dict() for i in intents],
        }

    def signal_book(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._signals[-max(1, int(limit)):]

    # ------------------------------------------------------------------
    def backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Research hook — returns a structured inert result until a real
        backtesting dataset is supplied via the governed channel."""
        strategy_id = str(payload.get("strategy_id") or "")
        if strategy_id and strategy_id not in self._strategies:
            return {
                "ok": False,
                "error_code": "STRATEGY_UNKNOWN",
                "strategy_id": strategy_id,
            }
        return {
            "ok": True,
            "strategy_id": strategy_id or "signal-follow",
            "status": "no-dataset",
            "note": (
                "Backtesting requires a governed dataset; supply "
                "'dataset' with bar series through ai-assistant."
            ),
            "requested_at": time.time(),
        }
