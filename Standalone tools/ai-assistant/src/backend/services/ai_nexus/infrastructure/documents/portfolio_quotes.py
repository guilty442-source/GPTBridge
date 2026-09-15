"""Quote lookup helpers split from the documents module."""
from __future__ import annotations

from .portfolio_models import *
from .portfolio_providers import *

def quote_holding(
    holding: Holding,
    providers: dict[str, QuoteProvider],
    provider_order: list[str],
    now: datetime,
) -> tuple[Quote | None, list[QuoteAttempt]]:
    quote, attempts, _candidates = quote_holding_candidates(
        holding,
        providers,
        provider_order,
        now,
        max_successes=1,
    )
    return quote, attempts


def quote_holding_candidates(
    holding: Holding,
    providers: dict[str, QuoteProvider],
    provider_order: list[str],
    now: datetime,
    *,
    max_successes: int = 2,
) -> tuple[Quote | None, list[QuoteAttempt], list[Quote]]:
    context = QuoteContext(holding=holding, now_utc=now)
    candidates: list[Quote] = []
    for provider_name in provider_order_for_holding(holding, provider_order):
        provider = providers.get(provider_name)
        if provider is None:
            context.attempts.append(
                QuoteAttempt(provider_name, False, "Provider is not available.")
            )
            continue
        if not provider.can_handle(holding):
            context.attempts.append(
                QuoteAttempt(provider.name, False, "Provider skipped this holding.")
            )
            continue
        try:
            quote = provider.quote(context)
        except (QuoteProviderError, urllib.error.URLError, TimeoutError, OSError) as exc:
            context.attempts.append(QuoteAttempt(provider.name, False, str(exc)))
            continue
        except Exception as exc:
            context.attempts.append(QuoteAttempt(provider.name, False, str(exc)))
            continue
        context.attempts.append(QuoteAttempt(provider.name, True, "ok"))
        candidates.append(quote)
        if len(candidates) >= max(1, max_successes):
            break
    primary_quote = candidates[0] if candidates else None
    return primary_quote, context.attempts, candidates


def quote_to_dict(quote: Quote) -> dict[str, Any]:
    return {
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
    }
