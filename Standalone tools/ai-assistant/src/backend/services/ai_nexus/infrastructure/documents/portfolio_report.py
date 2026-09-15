"""Portfolio report builders split from the documents module."""
from __future__ import annotations

from .portfolio_constants import *
from .portfolio_models import *
from .portfolio_utils import *

def round_number(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def holding_to_report(
    holding: Holding,
    quote: Quote | None,
    attempts: list[QuoteAttempt],
    now: datetime,
) -> dict[str, Any]:
    status = market_status(holding.market, now)
    base = {
        "symbol": holding.symbol,
        "name": holding.name,
        "market": holding.market,
        "asset_type": holding.asset_type,
        "quantity": round_number(holding.quantity),
        "average_cost": round_number(holding.average_cost),
        "currency": holding.currency,
        "market_status": status,
        "attempts": [
            {
                "provider": attempt.provider,
                "ok": attempt.ok,
                "message": attempt.message,
            }
            for attempt in attempts
        ],
    }
    if quote is None:
        return {
            **base,
            "status": "quote_failed",
            "quote": None,
            "market_value": None,
            "cost_basis": (
                round_number(holding.quantity * holding.average_cost)
                if holding.average_cost is not None
                else None
            ),
            "unrealized_pnl": None,
            "unrealized_pnl_percent": None,
        }

    market_value = holding.quantity * quote.price
    cost_basis = (
        holding.quantity * holding.average_cost
        if holding.average_cost is not None
        else None
    )
    pnl = market_value - cost_basis if cost_basis is not None else None
    pnl_percent = pnl / cost_basis * 100 if pnl is not None and cost_basis else None
    return {
        **base,
        "status": "quoted",
        "quote": {
            "symbol": quote.symbol,
            "requested_symbol": quote.requested_symbol,
            "provider": quote.provider,
            "price": round_number(quote.price),
            "currency": quote.currency,
            "previous_close": round_number(quote.previous_close),
            "change": round_number(quote.change),
            "change_percent": round_number(quote.change_percent),
            "as_of": quote.as_of,
            "market_state": quote.market_state,
            "exchange": quote.exchange,
            "raw_market": quote.raw_market,
        },
        "market_value": round_number(market_value),
        "cost_basis": round_number(cost_basis),
        "unrealized_pnl": round_number(pnl),
        "unrealized_pnl_percent": round_number(pnl_percent),
    }


def totals_by_currency(holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, float]] = {}
    for holding in holdings:
        quote = holding.get("quote") or {}
        currency = str(quote.get("currency") or holding.get("currency") or "UNKNOWN")
        bucket = totals.setdefault(
            currency,
            {
                "market_value": 0.0,
                "cost_basis": 0.0,
                "unrealized_pnl": 0.0,
                "quoted_count": 0,
            },
        )
        if isinstance(holding.get("market_value"), (int, float)):
            bucket["market_value"] += float(holding["market_value"])
        if isinstance(holding.get("cost_basis"), (int, float)):
            bucket["cost_basis"] += float(holding["cost_basis"])
        if isinstance(holding.get("unrealized_pnl"), (int, float)):
            bucket["unrealized_pnl"] += float(holding["unrealized_pnl"])
        if holding.get("status") == "quoted":
            bucket["quoted_count"] += 1
    return {
        currency: {
            key: int(value) if key == "quoted_count" else round_number(value, 4)
            for key, value in values.items()
        }
        for currency, values in totals.items()
    }


def markets_summary(holdings: list[Holding], now: datetime) -> dict[str, Any]:
    markets = sorted({holding.market for holding in holdings if holding.market})
    statuses = {market: market_status(market, now) for market in markets}
    open_markets = [
        market for market, status in statuses.items() if bool(status.get("is_open"))
    ]
    watchable_markets = [
        market
        for market in markets
        if market in MARKET_SESSIONS or market == "CRYPTO"
    ]
    return {
        "markets": statuses,
        "open_markets": open_markets,
        "watchable_markets": watchable_markets,
        "all_watchable_markets_closed": bool(watchable_markets)
        and not open_markets,
    }


def create_snapshot(
    holdings: list[Holding],
    providers: dict[str, QuoteProvider],
    provider_order: list[str],
) -> dict[str, Any]:
    now = utc_now()
    holding_reports: list[dict[str, Any]] = []
    for holding in holdings:
        quote, attempts = quote_holding(holding, providers, provider_order, now)
        holding_reports.append(holding_to_report(holding, quote, attempts, now))
    quoted_count = sum(1 for holding in holding_reports if holding["status"] == "quoted")
    summary = markets_summary(holdings, now)
    return {
        "timestamp": now.isoformat(),
        "holding_count": len(holding_reports),
        "quoted_count": quoted_count,
        "failed_quote_count": len(holding_reports) - quoted_count,
        "holdings": holding_reports,
        "totals_by_currency": totals_by_currency(holding_reports),
        "market_summary": summary,
    }
