"""CorporateActionService — splits, dividends, adjusted price series.

Every action keeps: raw prices untouched, an adjustment factor method,
the source, the effective date and a revision number. Adjusted series
are DERIVED — the store never rewrites raw candles; the adjustment is
computed on read, so backtests explicitly choose ``raw`` /
``split_adjusted`` / ``total_return`` and can never silently mix them.

Point-in-time safety: actions with ``effective_date`` after the
backtest's ``as_of`` are excluded — future corporate actions never leak
into history.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import AdjustmentType, MarketCandle


class ActionKind:
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    ETF_DISTRIBUTION = "etf_distribution"
    OTHER = "other"


@dataclass
class CorporateAction:
    instrument_id: str
    kind: str
    effective_date: date
    source_id: str
    ratio: Decimal = Decimal("1")       # split factor (2:1 → 2.0)
    cash_amount: Decimal = Decimal("0") # per-share cash dividend
    revision: int = 1
    action_id: str = field(
        default_factory=lambda: f"ca-{uuid.uuid4().hex[:12]}"
    )
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "instrument_id": self.instrument_id,
            "kind": self.kind,
            "effective_date": self.effective_date.isoformat(),
            "source_id": self.source_id,
            "ratio": str(self.ratio),
            "cash_amount": str(self.cash_amount),
            "revision": self.revision,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CorporateAction":
        return cls(
            action_id=row["action_id"],
            instrument_id=row["instrument_id"],
            kind=row["kind"],
            effective_date=date.fromisoformat(row["effective_date"]),
            source_id=row["source_id"],
            ratio=Decimal(str(row["ratio"])),
            cash_amount=Decimal(str(row.get("cash_amount") or 0)),
            revision=int(row.get("revision") or 1),
            recorded_at=float(row.get("recorded_at") or 0.0),
        )


class CorporateActionService:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "corporate-actions.jsonl"

    # ------------------------------------------------------------------
    def record(self, action: CorporateAction) -> dict[str, Any]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(action.to_dict(), ensure_ascii=False) + "\n")
        return {"ok": True, "action": action.to_dict()}

    def actions(
        self,
        instrument_id: str | None = None,
        as_of: date | None = None,
    ) -> list[CorporateAction]:
        """Known actions — ``as_of`` excludes not-yet-effective ones
        (no lookahead into history)."""
        out: list[CorporateAction] = []
        if not self._path.is_file():
            return out
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                action = CorporateAction.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if instrument_id and action.instrument_id != instrument_id:
                continue
            if as_of and action.effective_date > as_of:
                continue
            out.append(action)
        return out

    # ------------------------------------------------------------------
    def adjust(
        self,
        candles: list[MarketCandle],
        adjustment_type: str,
        as_of: date | None = None,
    ) -> list[MarketCandle]:
        """Derive an adjusted series — caller must name the price type.

        ``raw`` returns the input untouched. Mixing is impossible: the
        result carries ``adjustment_type`` on every candle.
        """
        if adjustment_type == AdjustmentType.RAW.value:
            return list(candles)
        if adjustment_type not in (
            AdjustmentType.SPLIT_ADJUSTED.value,
            AdjustmentType.TOTAL_RETURN.value,
        ):
            raise ValueError(f"unknown adjustment_type {adjustment_type!r}")

        out: list[MarketCandle] = []
        for candle in candles:
            factor = Decimal("1")
            cash_adj = Decimal("0")
            for action in self.actions(candle.instrument_id, as_of=as_of):
                # Only actions effective AFTER the candle adjust it
                # backwards (standard back-adjustment).
                if action.effective_date <= candle.candle_start.date():
                    continue
                if action.kind in (ActionKind.SPLIT, ActionKind.REVERSE_SPLIT):
                    factor /= action.ratio
                if adjustment_type == AdjustmentType.TOTAL_RETURN.value:
                    if action.kind in (
                        ActionKind.CASH_DIVIDEND,
                        ActionKind.STOCK_DIVIDEND,
                        ActionKind.ETF_DISTRIBUTION,
                    ):
                        cash_adj += action.cash_amount
            if factor == 1 and cash_adj == 0:
                out.append(candle)
                continue
            adjusted = MarketCandle(
                instrument_id=candle.instrument_id,
                market=candle.market,
                timeframe=candle.timeframe,
                open=candle.open * factor - cash_adj,
                high=candle.high * factor - cash_adj,
                low=candle.low * factor - cash_adj,
                close=candle.close * factor - cash_adj,
                volume=candle.volume / factor if factor else candle.volume,
                turnover=candle.turnover,
                currency=candle.currency,
                candle_start=candle.candle_start,
                candle_end=candle.candle_end,
                source_id=candle.source_id,
                data_revision=candle.data_revision,
                adjustment_type=adjustment_type,
            )
            out.append(adjusted)
        return out
