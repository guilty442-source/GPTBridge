"""CurrencyRateService — TWD/USD conversion with provenance.

Phase 1 supports TWD ↔ USD. Every rate carries source + timestamp +
validity; conversions return Decimal. Account cash is NEVER rewritten
into converted amounts — conversion is a read-time projection for the
asset overview only; per-account ledgers keep their native currency.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class CurrencyRate:
    base: str                       # e.g. USD
    quote: str                      # e.g. TWD
    rate: Decimal                   # 1 base = rate quote
    source_id: str
    observed_at: datetime
    valid_until: datetime | None = None
    rate_id: str = field(
        default_factory=lambda: f"fx-{uuid.uuid4().hex[:12]}"
    )

    def is_valid(self, at: datetime | None = None) -> bool:
        if self.valid_until is None:
            return True
        return (at or datetime.now(timezone.utc)) <= self.valid_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_id": self.rate_id,
            "base": self.base,
            "quote": self.quote,
            "rate": str(self.rate),
            "source_id": self.source_id,
            "observed_at": self.observed_at.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }


class CurrencyRateService:
    SUPPORTED = {"TWD", "USD"}

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "currency-rates.jsonl"

    # ------------------------------------------------------------------
    def record_rate(
        self,
        base: str,
        quote: str,
        rate: Decimal | str | float,
        source_id: str,
        observed_at: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> dict[str, Any]:
        base, quote = base.upper(), quote.upper()
        if base not in self.SUPPORTED or quote not in self.SUPPORTED:
            return {"ok": False, "error_code": "CURRENCY_UNSUPPORTED"}
        if base == quote:
            return {"ok": False, "error_code": "SAME_CURRENCY"}
        rec = CurrencyRate(
            base=base, quote=quote, rate=Decimal(str(rate)),
            source_id=str(source_id),
            observed_at=observed_at or datetime.now(timezone.utc),
            valid_until=valid_until,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        return {"ok": True, "rate": rec.to_dict()}

    # ------------------------------------------------------------------
    def _rates(self) -> list[CurrencyRate]:
        out: list[CurrencyRate] = []
        if not self._path.is_file():
            return out
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                out.append(CurrencyRate(
                    rate_id=row["rate_id"],
                    base=row["base"], quote=row["quote"],
                    rate=Decimal(row["rate"]),
                    source_id=row["source_id"],
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                    valid_until=(
                        datetime.fromisoformat(row["valid_until"])
                        if row.get("valid_until") else None
                    ),
                ))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return out

    def latest(self, base: str, quote: str) -> CurrencyRate | None:
        base, quote = base.upper(), quote.upper()
        candidates = [
            r for r in self._rates()
            if r.base == base and r.quote == quote and r.is_valid()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.observed_at)

    def history(self, base: str, quote: str) -> list[dict[str, Any]]:
        base, quote = base.upper(), quote.upper()
        return [
            r.to_dict() for r in self._rates()
            if r.base == base and r.quote == quote
        ]

    # ------------------------------------------------------------------
    def convert(
        self,
        amount: Decimal | str | float,
        from_currency: str,
        to_currency: str,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Read-time conversion — never rewrites account balances."""
        from_c, to_c = from_currency.upper(), to_currency.upper()
        value = Decimal(str(amount))
        if from_c == to_c:
            return {
                "ok": True, "amount": str(value), "currency": to_c,
                "rate": "1", "source_id": "identity",
            }
        direct = self.latest(from_c, to_c)
        if direct is not None:
            converted = value * direct.rate
            return {
                "ok": True, "amount": str(converted), "currency": to_c,
                "rate": str(direct.rate), "source_id": direct.source_id,
                "observed_at": direct.observed_at.isoformat(),
            }
        inverse = self.latest(to_c, from_c)
        if inverse is not None:
            rate = Decimal(1) / inverse.rate
            converted = value * rate
            return {
                "ok": True, "amount": str(converted), "currency": to_c,
                "rate": str(rate), "source_id": inverse.source_id,
                "observed_at": inverse.observed_at.isoformat(),
                "inverted": True,
            }
        return {"ok": False, "error_code": "RATE_UNAVAILABLE"}
