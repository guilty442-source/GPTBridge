"""mutual-fund domain — fund ledger + manual data import.

The provider platform is undecided — data enters via manual import
(NAV observations, subscription/redemption transactions) until a
provider API is selected and verified through the broker-adapter
verification path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class FundLedger:
    """Manual-import fund transactions and NAV history."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir
        self._nav_path = state_dir / "fund-nav.jsonl"
        self._txn_path = state_dir / "fund-transactions.jsonl"

    # ------------------------------------------------------------------
    def import_nav(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Manual NAV import — instrument_id must be a fund identity."""
        instrument_id = str(payload.get("instrument_id") or "")
        nav = float(payload.get("nav") or 0.0)
        nav_date = str(payload.get("nav_date") or "")
        if not instrument_id.startswith("fund:") or nav <= 0:
            return {"ok": False, "error_code": "INVALID_FUND_NAV"}
        row = {
            "instrument_id": instrument_id,
            "nav": nav,
            "nav_date": nav_date,
            "currency": str(payload.get("currency") or ""),
            "source": str(payload.get("source") or "manual"),
            "imported_at": time.time(),
        }
        self._append(self._nav_path, row)
        return {"ok": True, "nav": row}

    def import_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Manual subscription/redemption record (user-entered)."""
        kind = str(payload.get("kind") or "")
        instrument_id = str(payload.get("instrument_id") or "")
        units = float(payload.get("units") or 0.0)
        amount = float(payload.get("amount") or 0.0)
        if kind not in ("subscription", "redemption") or not instrument_id:
            return {"ok": False, "error_code": "INVALID_FUND_TRANSACTION"}
        row = {
            "instrument_id": instrument_id,
            "kind": kind,
            "units": units,
            "amount": amount,
            "currency": str(payload.get("currency") or ""),
            "account_id": str(payload.get("account_id") or "fund-provider-generic"),
            "trade_date": str(payload.get("trade_date") or ""),
            "source": str(payload.get("source") or "manual"),
            "imported_at": time.time(),
        }
        self._append(self._txn_path, row)
        return {"ok": True, "transaction": row}

    # ------------------------------------------------------------------
    def nav_history(self, instrument_id: str) -> list[dict[str, Any]]:
        return [
            r for r in self._read(self._nav_path)
            if r.get("instrument_id") == instrument_id
        ]

    def transactions(self, instrument_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._read(self._txn_path)
        if instrument_id:
            rows = [r for r in rows if r.get("instrument_id") == instrument_id]
        return rows

    def units_held(self, instrument_id: str) -> float:
        units = 0.0
        for row in self._read(self._txn_path):
            if row.get("instrument_id") != instrument_id:
                continue
            delta = float(row.get("units") or 0.0)
            units += delta if row.get("kind") == "subscription" else -delta
        return max(units, 0.0)

    # ------------------------------------------------------------------
    @staticmethod
    def _append(path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for raw in lines:
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return rows
