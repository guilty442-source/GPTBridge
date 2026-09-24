"""PositionManagementService — position view over the offline journal.

Position contract (all decimal-string quantities — float is never the
authoritative basis for money math):

    position_id, account_id, instrument_id,
    quantity, available_quantity, reserved_quantity,
    average_cost, cost_currency, market_price, market_value,
    unrealized_pnl, valuation_timestamp, data_source, data_status

``data_status``: ``manual`` | ``imported`` | ``estimated`` — never
``broker_confirmed``. Reserved quantities live in a side journal so a
pending order can earmark shares without rewriting the book.
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

# Per-market precision/trading-unit rules.
MARKET_RULES: dict[str, dict[str, Any]] = {
    "tw": {"qty_places": 0, "price_places": 2, "currency": "TWD",
           "unit": 1000},
    "us": {"qty_places": 0, "price_places": 2, "currency": "USD",
           "unit": 1},
    "fund": {"qty_places": 4, "price_places": 4, "currency": "",
             "unit": 0},
}


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class PositionManagementService:
    def __init__(self, state_dir: Path, offline: Any) -> None:
        self._offline = offline
        self._res_path = state_dir / "asset-reservations.json"
        self._reservations: dict[str, dict[str, Decimal]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._res_path.read_text(encoding="utf-8"))
            for aid, rows in data.items():
                self._reservations[aid] = {
                    k: Decimal(str(v)) for k, v in rows.items()}
        except Exception:
            self._reservations = {}

    def _persist(self) -> None:
        self._res_path.write_text(json.dumps(
            {a: {k: str(v) for k, v in rows.items()}
             for a, rows in self._reservations.items()},
            indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def reserve(self, account_id: str, instrument_id: str,
                quantity) -> dict[str, Any]:
        pos = self.get(account_id, instrument_id)
        if pos is None:
            return {"ok": False, "error_code": "POSITION_NOT_FOUND"}
        qty = _d(quantity)
        if qty <= 0 or qty > _d(pos["available_quantity"]):
            return {"ok": False,
                    "error_code": "INSUFFICIENT_AVAILABLE"}
        self._reservations.setdefault(account_id, {})[instrument_id] = \
            self._reservations.setdefault(account_id, {}).get(
                instrument_id, Decimal(0)) + qty
        self._persist()
        return {"ok": True}

    def release(self, account_id: str, instrument_id: str,
                quantity) -> dict[str, Any]:
        rows = self._reservations.get(account_id, {})
        cur = rows.get(instrument_id, Decimal(0))
        qty = min(_d(quantity), cur)
        rows[instrument_id] = cur - qty
        if rows[instrument_id] <= 0:
            rows.pop(instrument_id, None)
        self._persist()
        return {"ok": True}

    # ------------------------------------------------------------------
    def get(self, account_id: str,
            instrument_id: str) -> dict[str, Any] | None:
        for h in self._offline.holdings(account_id):
            if h["instrument_id"] == instrument_id:
                return self._to_position(account_id, h)
        return None

    def list_positions(self, account_id: str | None = None
                       ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for acct in self._offline.list_accounts():
            aid = acct["account_id"]
            if account_id and aid != account_id:
                continue
            for h in self._offline.holdings(aid):
                out.append(self._to_position(aid, h))
        return out

    def _to_position(self, account_id: str,
                     h: dict[str, Any]) -> dict[str, Any]:
        qty = _d(h["quantity"])
        reserved = self._reservations.get(
            account_id, {}).get(h["instrument_id"], Decimal(0))
        market = h.get("market") or ""
        rules = MARKET_RULES.get(market, {})
        return {
            "position_id": f"pos-{account_id}-{h['instrument_id']}",
            "account_id": account_id,
            "instrument_id": h["instrument_id"],
            "quantity": str(qty),
            "available_quantity": str(max(Decimal(0), qty - reserved)),
            "reserved_quantity": str(reserved),
            "average_cost": h["avg_cost"],
            "cost_currency": h.get("currency", ""),
            "market_price": "",          # filled by valuation engine
            "market_value": "",
            "unrealized_pnl": "",
            "valuation_timestamp": h.get("updated_at", 0),
            "data_source": h.get("source", "MANUAL"),
            "data_status": ("imported" if h.get("source") == "FILE_IMPORT"
                            else "manual"),
            "broker_confirmed": False,
            "market": market,
            "market_rules": rules,
        }
