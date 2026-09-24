"""market-data domain — observation cache (non-authoritative).

Market observations enter through the governed channel (星澄/embedded
browser owns market search; this tool never opens market connections).
The cache feeds the strategy engine and PAPER-mode simulated fills.
Stale observations are rejected by the risk engine via ``max_age_s``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import MarketObservation


class MarketDataCache:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "market-observations.json"
        self._latest: dict[str, MarketObservation] = {}
        self._load()

    def _load(self) -> None:
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                obs = MarketObservation(**row)
                self._latest[obs.instrument_id] = obs
        except Exception:
            pass

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([o.to_dict() for o in self._latest.values()], ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    def observe(self, payload: dict[str, Any]) -> dict[str, Any]:
        obs = MarketObservation(
            instrument_id=str(payload.get("instrument_id") or ""),
            price=float(payload.get("price") or 0.0),
            currency=str(payload.get("currency") or ""),
            source=str(payload.get("source") or "xingcheng"),
            kind=str(payload.get("kind") or "quote"),
            payload=dict(payload.get("detail") or {}),
        )
        if not obs.instrument_id or obs.price <= 0:
            return {"ok": False, "error_code": "INVALID_OBSERVATION"}
        self._latest[obs.instrument_id] = obs
        self._persist()
        return {"ok": True, "observation": obs.to_dict()}

    def latest(self, instrument_id: str) -> dict[str, Any] | None:
        obs = self._latest.get(str(instrument_id))
        return obs.to_dict() if obs else None

    def price(self, instrument_id: str, max_age_s: float = 3600.0) -> float | None:
        """Latest price or None when missing/stale — callers fail closed."""
        obs = self._latest.get(str(instrument_id))
        if obs is None:
            return None
        if time.time() - obs.observed_at > max_age_s:
            return None
        return obs.price

    def all(self) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self._latest.values()]
