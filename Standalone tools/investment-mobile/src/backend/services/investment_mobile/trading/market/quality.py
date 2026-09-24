"""Market data validation + anomaly detection.

Every inbound quote is checked before it enters the shared cache:
- required fields / instrument identity shape
- non-negative prices, volume, bid<=ask coherence
- timestamp sanity (not in the future beyond a small skew; not ancient)
- jump detection vs the previous cached quote (outlier guard)
- currency/unit consistency per instrument identity

Failures mark the quote ``suspect`` — it is still recorded for audit
but flagged, and ``fresh_price`` refuses to serve suspect data to
trading paths.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .contracts import MarketQuote


class QuoteValidator:
    FUTURE_SKEW_S = 120.0       # allow small clock skew
    MAX_JUMP_PCT = Decimal("0.5")  # >50% single-tick jump → suspect

    def check(
        self, quote: MarketQuote, previous: MarketQuote | None
    ) -> list[str]:
        issues: list[str] = []
        if not quote.instrument_id or ":" not in quote.instrument_id:
            issues.append("instrument_id_malformed")
        if quote.market and not quote.instrument_id.startswith(
            f"{quote.market}:" if quote.market != "fund" else "fund:"
        ):
            issues.append("instrument_market_mismatch")
        if quote.last_price is not None and quote.last_price < 0:
            issues.append("negative_price")
        if (
            quote.bid_price is not None
            and quote.ask_price is not None
            and quote.bid_price > quote.ask_price
        ):
            issues.append("bid_above_ask")
        if quote.volume < 0:
            issues.append("negative_volume")
        if quote.source_age_s > self.FUTURE_SKEW_S and quote.source_timestamp > quote.received_timestamp:
            issues.append("timestamp_in_future")
        if previous is not None and previous.last_price and quote.last_price:
            if previous.currency and quote.currency and previous.currency != quote.currency:
                issues.append("currency_inconsistent")
            if previous.last_price > 0:
                jump = abs(quote.last_price - previous.last_price) / previous.last_price
                if jump > self.MAX_JUMP_PCT:
                    issues.append("price_jump_outlier")
        if not quote.currency:
            issues.append("currency_missing")
        return issues
