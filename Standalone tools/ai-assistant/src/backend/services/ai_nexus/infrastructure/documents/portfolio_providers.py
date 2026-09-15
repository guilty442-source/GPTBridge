"""Quote provider implementations split from the documents module."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

from .portfolio_constants import *
from .portfolio_models import *
from .portfolio_utils import *

def request_text(url: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> str:
    raise PermissionError(
        "AI investment manager has no network access; Xingcheng must provide market data."
    )


def request_json(url: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> Any:
    return json.loads(request_text(url, timeout=timeout))


def parse_unix_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return utc_now_text()
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat()


def market_status(market: str, now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or utc_now()
    if market == "CRYPTO":
        return {
            "market": market,
            "is_open": True,
            "state": "open",
            "timezone": "UTC",
            "local_time": now.isoformat(),
        }
    session_info = MARKET_SESSIONS.get(market)
    if not session_info:
        return {
            "market": market,
            "is_open": False,
            "state": "snapshot_only",
            "timezone": "UTC",
            "local_time": now.isoformat(),
        }
    zone = timezone_for(str(session_info["timezone"]), now)
    local_now = now.astimezone(zone)
    is_weekday = local_now.weekday() < 5
    is_open = False
    if is_weekday:
        for start, end in session_info["sessions"]:
            if start <= local_now.time() <= end:
                is_open = True
                break
    return {
        "market": market,
        "is_open": is_open,
        "state": "open" if is_open else "closed",
        "timezone": str(session_info["timezone"]),
        "local_time": local_now.isoformat(),
    }


def yahoo_symbol(holding: Holding) -> str:
    symbol = holding.symbol.upper()
    if holding.market == "TW" and not symbol.endswith((".TW", ".TWO")):
        return f"{symbol}.TW"
    if holding.market == "HK" and not symbol.endswith(".HK"):
        if symbol.isdigit():
            return f"{int(symbol):04d}.HK"
        return f"{symbol}.HK"
    if holding.market == "CRYPTO":
        if "/" in symbol:
            base, quote = symbol.split("/", 1)
            return f"{base}-{quote or 'USD'}"
        if "-" not in symbol:
            return f"{symbol}-USD"
    return symbol


class QuoteProvider:
    name = "base"

    def can_handle(self, holding: Holding) -> bool:
        return True

    def quote(self, context: QuoteContext) -> Quote:
        raise NotImplementedError


class YahooChartProvider(QuoteProvider):
    name = "yahoo-chart"

    def quote(self, context: QuoteContext) -> Quote:
        requested = yahoo_symbol(context.holding)
        encoded = urllib.parse.quote(requested, safe="")
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{encoded}?range=1d&interval=1m"
        )
        payload = request_json(url)
        chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
        error = chart.get("error")
        if error:
            raise QuoteProviderError(str(error))
        results = chart.get("result") or []
        if not results:
            raise QuoteProviderError("No chart result.")
        meta = results[0].get("meta", {})
        price = parse_float(meta.get("regularMarketPrice"))
        if price is None:
            indicators = results[0].get("indicators", {}).get("quote", [])
            closes = indicators[0].get("close", []) if indicators else []
            numeric_closes = [parse_float(value) for value in closes]
            numeric_closes = [value for value in numeric_closes if value is not None]
            price = numeric_closes[-1] if numeric_closes else None
        if price is None:
            raise QuoteProviderError("No market price.")
        previous_close = parse_float(meta.get("chartPreviousClose"))
        change = price - previous_close if previous_close is not None else None
        change_percent = (
            change / previous_close * 100
            if change is not None and previous_close
            else None
        )
        as_of = parse_unix_timestamp(meta.get("regularMarketTime"))
        return Quote(
            symbol=str(meta.get("symbol") or requested),
            requested_symbol=requested,
            provider=self.name,
            price=price,
            currency=str(meta.get("currency") or context.holding.currency or ""),
            previous_close=previous_close,
            change=change,
            change_percent=change_percent,
            as_of=as_of,
            market_state=str(meta.get("marketState") or ""),
            exchange=str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
            raw_market=str(meta.get("exchangeTimezoneName") or ""),
        )


class YahooQuoteProvider(QuoteProvider):
    name = "yahoo-quote"

    def quote(self, context: QuoteContext) -> Quote:
        requested = yahoo_symbol(context.holding)
        encoded = urllib.parse.quote(requested, safe=",")
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={encoded}"
        payload = request_json(url)
        results = (
            payload.get("quoteResponse", {}).get("result", [])
            if isinstance(payload, dict)
            else []
        )
        if not results:
            raise QuoteProviderError("No quote result.")
        item = results[0]
        price = parse_float(item.get("regularMarketPrice"))
        if price is None:
            raise QuoteProviderError("No regular market price.")
        previous_close = parse_float(item.get("regularMarketPreviousClose"))
        return Quote(
            symbol=str(item.get("symbol") or requested),
            requested_symbol=requested,
            provider=self.name,
            price=price,
            currency=str(item.get("currency") or context.holding.currency or ""),
            previous_close=previous_close,
            change=parse_float(item.get("regularMarketChange")),
            change_percent=parse_float(item.get("regularMarketChangePercent")),
            as_of=parse_unix_timestamp(item.get("regularMarketTime")),
            market_state=str(item.get("marketState") or ""),
            exchange=str(item.get("fullExchangeName") or item.get("exchange") or ""),
        )


class CoinGeckoProvider(QuoteProvider):
    name = "coingecko"

    def can_handle(self, holding: Holding) -> bool:
        return holding.market == "CRYPTO"

    def quote(self, context: QuoteContext) -> Quote:
        symbol = context.holding.symbol.upper().split("-", 1)[0].split("/", 1)[0]
        coin_id = CRYPTO_ID_MAP.get(symbol)
        if not coin_id:
            raise QuoteProviderError(f"Unsupported crypto symbol: {symbol}")
        currency = (context.holding.currency or "USD").lower()
        url = (
            "https://api.coingecko.com/api/v3/simple/price?"
            + urllib.parse.urlencode(
                {
                    "ids": coin_id,
                    "vs_currencies": currency,
                    "include_24hr_change": "true",
                }
            )
        )
        payload = request_json(url)
        item = payload.get(coin_id, {}) if isinstance(payload, dict) else {}
        price = parse_float(item.get(currency))
        if price is None:
            raise QuoteProviderError("No crypto price.")
        return Quote(
            symbol=symbol,
            requested_symbol=coin_id,
            provider=self.name,
            price=price,
            currency=currency.upper(),
            change_percent=parse_float(item.get(f"{currency}_24h_change")),
            as_of=utc_now_text(),
            market_state="REGULAR",
            exchange="CoinGecko",
        )


class AlphaVantageProvider(QuoteProvider):
    name = "alphavantage"

    def can_handle(self, holding: Holding) -> bool:
        return bool(os.environ.get("ALPHAVANTAGE_API_KEY"))

    def quote(self, context: QuoteContext) -> Quote:
        api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
        if not api_key:
            raise QuoteProviderError("ALPHAVANTAGE_API_KEY is not configured.")
        requested = yahoo_symbol(context.holding)
        if requested.endswith((".TW", ".TWO", ".HK")):
            requested = context.holding.symbol
        url = (
            "https://www.alphavantage.co/query?"
            + urllib.parse.urlencode(
                {
                    "function": "GLOBAL_QUOTE",
                    "symbol": requested,
                    "apikey": api_key,
                }
            )
        )
        payload = request_json(url)
        item = payload.get("Global Quote", {}) if isinstance(payload, dict) else {}
        price = parse_float(item.get("05. price"))
        if price is None:
            raise QuoteProviderError("No Alpha Vantage price.")
        previous_close = parse_float(item.get("08. previous close"))
        return Quote(
            symbol=str(item.get("01. symbol") or requested),
            requested_symbol=requested,
            provider=self.name,
            price=price,
            currency=context.holding.currency,
            previous_close=previous_close,
            change=parse_float(item.get("09. change")),
            change_percent=parse_float(item.get("10. change percent")),
            as_of=utc_now_text(),
            market_state="",
            exchange="Alpha Vantage",
        )


class TwseProvider(QuoteProvider):
    name = "twse"

    def can_handle(self, holding: Holding) -> bool:
        return holding.market == "TW" and bool(re.fullmatch(r"\d{4,6}", holding.symbol))

    def quote(self, context: QuoteContext) -> Quote:
        errors: list[str] = []
        for exchange_prefix in ("tse", "otc"):
            ex_ch = f"{exchange_prefix}_{context.holding.symbol}.tw"
            url = (
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?"
                + urllib.parse.urlencode({"ex_ch": ex_ch, "json": "1", "delay": "0"})
            )
            try:
                payload = request_json(url)
            except Exception as exc:
                errors.append(str(exc))
                continue
            rows = payload.get("msgArray", []) if isinstance(payload, dict) else []
            if not rows:
                errors.append("No TWSE quote rows.")
                continue
            item = rows[0]
            price = parse_float(item.get("z")) or parse_float(item.get("y"))
            if price is None:
                errors.append("No TWSE price.")
                continue
            previous_close = parse_float(item.get("y"))
            trade_date = str(item.get("d") or "")
            trade_time = str(item.get("t") or "")
            as_of = utc_now_text()
            if re.fullmatch(r"\d{8}", trade_date) and trade_time:
                try:
                    local_dt = datetime.strptime(
                        f"{trade_date} {trade_time}",
                        "%Y%m%d %H:%M:%S",
                    ).replace(tzinfo=timezone_for("Asia/Taipei", context.now_utc))
                    as_of = local_dt.astimezone().isoformat()
                except ValueError:
                    pass
            return Quote(
                symbol=str(item.get("c") or context.holding.symbol),
                requested_symbol=ex_ch,
                provider=self.name,
                price=price,
                currency="TWD",
                previous_close=previous_close,
                change=price - previous_close if previous_close is not None else None,
                change_percent=(
                    (price - previous_close) / previous_close * 100
                    if previous_close
                    else None
                ),
                as_of=as_of,
                market_state=market_status("TW", context.now_utc)["state"].upper(),
                exchange="TWSE" if exchange_prefix == "tse" else "TPEx",
            )
        raise QuoteProviderError("; ".join(errors) or "TWSE quote failed.")


def provider_registry() -> dict[str, QuoteProvider]:
    providers: list[QuoteProvider] = [
        TwseProvider(),
        YahooChartProvider(),
        YahooQuoteProvider(),
        CoinGeckoProvider(),
        AlphaVantageProvider(),
    ]
    return {provider.name: provider for provider in providers}


def provider_order_for_holding(
    holding: Holding,
    requested_order: list[str],
) -> list[str]:
    preferred = list(requested_order)
    if holding.market == "CRYPTO":
        preferred = ["coingecko", *[item for item in preferred if item != "coingecko"]]
    elif holding.market == "TW":
        preferred = ["twse", *[item for item in preferred if item != "twse"]]
    return list(dict.fromkeys(preferred))
