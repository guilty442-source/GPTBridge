"""HistoricalUniverseService — point-in-time membership + survivorship.

Without a delisting/constituent source the universe is *declared*:
backtests over it carry SURVIVORSHIP_BIAS_RISK until a verified
historical membership source is registered. Never present a survivorship-
biased run as full-market validation.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


class HistoricalUniverseService:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "universe_membership.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def register_membership(self, index_or_market: str, instrument_id: str,
                            listed_from: date, listed_to: date | None,
                            source_id: str) -> dict[str, Any]:
        self._append({
            "index_or_market": index_or_market,
            "instrument_id": instrument_id,
            "listed_from": str(listed_from),
            "listed_to": str(listed_to) if listed_to else None,
            "source_id": source_id,
        })
        return {"ok": True}

    def members(self, index_or_market: str, at: date) -> list[str]:
        out = []
        for row in self._read():
            if row["index_or_market"] != index_or_market:
                continue
            if row["listed_from"] <= str(at) and (
                    row["listed_to"] is None or str(at) <= row["listed_to"]):
                out.append(row["instrument_id"])
        return out

    def survivorship_flag(self, index_or_market: str) -> bool:
        """True when no verified membership data exists for this market."""
        return not any(
            r["index_or_market"] == index_or_market for r in self._read())

    def _read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return [json.loads(l) for l in
                self._path.read_text("utf-8").splitlines() if l.strip()]

    def _append(self, row: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
