"""instrument domain — registry of tradable instruments.

Collision-free identity (``Instrument.make_id``): market + type + symbol
+ currency for exchange-traded instruments; fund_id + share_class +
currency for mutual funds — different markets, currencies and share
classes can never collide.

The registry is the tool-local runtime mirror; the authoritative
``gptbridge_trading.instrument`` table lives in PostgreSQL via
ai-assistant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import FundDetails, Instrument, InstrumentType


class InstrumentRegistry:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "instruments.json"
        self._items: dict[str, Instrument] = {}
        self._load()

    def _load(self) -> None:
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                fund = (
                    FundDetails(**row["fund"]) if row.get("fund") else None
                )
                kwargs = {k: v for k, v in row.items() if k not in ("fund", "instrument_id")}
                instrument = Instrument(fund=fund, **kwargs)
                self._items[instrument.instrument_id] = instrument
            except (TypeError, KeyError):
                continue

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([i.to_dict() for i in self._items.values()], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        fund_payload = payload.get("fund")
        fund = FundDetails(**fund_payload) if isinstance(fund_payload, dict) else None
        instrument_type = str(payload.get("instrument_type") or "")
        if instrument_type not in {t.value for t in InstrumentType}:
            return {"ok": False, "error_code": "INSTRUMENT_TYPE_UNKNOWN"}
        if instrument_type == InstrumentType.MUTUAL_FUND.value and fund is None:
            return {"ok": False, "error_code": "FUND_DETAILS_REQUIRED"}
        instrument = Instrument(
            instrument_type=instrument_type,
            market=str(payload.get("market") or ""),
            symbol=str(payload.get("symbol") or ""),
            currency=str(payload.get("currency") or ""),
            display_name=str(payload.get("display_name") or ""),
            isin=str(payload.get("isin") or ""),
            exchange=str(payload.get("exchange") or ""),
            trading_calendar=str(payload.get("trading_calendar") or ""),
            price_precision=int(payload.get("price_precision") or 2),
            quantity_precision=int(payload.get("quantity_precision") or 0),
            status=str(payload.get("status") or "active"),
            fund=fund,
        )
        self._items[instrument.instrument_id] = instrument
        self._persist()
        return {"ok": True, "instrument": instrument.to_dict()}

    def get(self, instrument_id: str) -> dict[str, Any] | None:
        instrument = self._items.get(str(instrument_id))
        return instrument.to_dict() if instrument else None

    def list(self, market: str | None = None) -> list[dict[str, Any]]:
        items = self._items.values()
        if market:
            items = [i for i in items if i.market == market]
        return [i.to_dict() for i in items]
