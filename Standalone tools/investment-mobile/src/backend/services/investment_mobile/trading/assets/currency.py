"""CurrencyConversionEngine — TWD/USD conversions with rate dating.

Wraps the market ``CurrencyRateService``:

- latest known rate with its actual ``observed_at`` date,
- historical rate lookup for a valuation date (nearest rate at or
  before that date),
- conversion-cost tracking (spread recorded on fx_exchange
  transactions).

Underlying accounts keep their original currency — conversion is a
read-time view, never a rewrite of the books.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class CurrencyConversionEngine:
    SUPPORTED = ("TWD", "USD")

    def __init__(self, fx: Any) -> None:
        self._fx = fx

    # ------------------------------------------------------------------
    def convert(self, amount, from_currency: str, to_currency: str,
                *, at: float | None = None) -> dict[str, Any]:
        """Convert using latest rate, or the rate valid at ``at``."""
        from_c, to_c = from_currency.upper(), to_currency.upper()
        if from_c == to_c:
            return {"ok": True, "amount": str(_d(amount)),
                    "currency": to_c, "rate": "1",
                    "rate_date": None}
        if at is not None:
            rate = self._rate_at(from_c, to_c, at)
            if rate is None:
                return {"ok": False, "error_code": "RATE_UNAVAILABLE",
                        "pair": f"{from_c}->{to_c}", "at": at}
            return {"ok": True,
                    "amount": str(_d(amount) * rate["rate"]),
                    "currency": to_c, "rate": str(rate["rate"]),
                    "rate_date": rate["observed_at"],
                    "historical": True}
        r = self._fx.convert(amount, from_c, to_c)
        if r.get("ok"):
            r["rate_date"] = r.get("observed_at")
        return r

    def _rate_at(self, base: str, quote: str,
                 at: float) -> dict[str, Any] | None:
        """Latest recorded rate on or before ``at`` — never newer."""
        best = None
        for row in self._fx.history(base, quote):
            try:
                ts = datetime.fromisoformat(
                    row["observed_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts <= at and (best is None or ts > best[0]):
                best = (ts, row)
        if best is not None:
            return {"rate": _d(best[1]["rate"]),
                    "observed_at": best[1]["observed_at"]}
        inv = self._fx.history(quote, base)
        best = None
        for row in inv:
            try:
                ts = datetime.fromisoformat(
                    row["observed_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts <= at and (best is None or ts > best[0]):
                best = (ts, row)
        if best is not None:
            return {"rate": Decimal(1) / _d(best[1]["rate"]),
                    "observed_at": best[1]["observed_at"],
                    "inverted": True}
        return None

    def staleness(self, base: str, quote: str) -> dict[str, Any]:
        r = self._fx.latest(base, quote) or self._fx.latest(quote, base)
        if r is None:
            return {"ok": False, "error_code": "RATE_UNAVAILABLE",
                    "pair": f"{base}->{quote}"}
        age = (datetime.now(timezone.utc) - r.observed_at).total_seconds()
        return {"ok": True, "pair": f"{base}->{quote}",
                "rate": str(r.rate), "observed_at": r.observed_at,
                "age_seconds": age, "stale": age > 7 * 86400}
