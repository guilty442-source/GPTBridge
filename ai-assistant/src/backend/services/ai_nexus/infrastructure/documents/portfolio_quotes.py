"""Split from portfolio_file.py."""
from __future__ import annotations

from .portfolio_models import *
from .portfolio_io import *

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time as local_time, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, Iterator
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


PROGRESS_JSON_PREFIX = "INVESTMENT_MANAGER_PROGRESS_JSON="
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 12
DEFAULT_PROVIDER_ORDER = (
    "twse",
    "yahoo-chart",
    "yahoo-quote",
    "coingecko",
    "alphavantage",
)
DEFAULT_IMPORT_SNAPSHOT_KEEP = 20
SNAPSHOT_COPY_ATTEMPTS = 3
SNAPSHOT_COPY_CHUNK_BYTES = 1024 * 1024
XLSX_INFERRED_HEADER_SCAN_ROWS = 25
XLSX_HEADER_SCAN_MAX_ROWS = 80
XLSX_HEADER_SCAN_MAX_COLUMNS = 96
XLSX_HORIZONTAL_GROUP_WIDTH = 4
CSV_EXTENSIONS = {".csv", ".tsv", ".txt"}
JSON_EXTENSIONS = {".json"}
XLSX_EXTENSIONS = {".xlsx"}
LEGACY_EXCEL_EXTENSIONS = {".xls"}
EXCEL_EXTENSIONS = XLSX_EXTENSIONS | LEGACY_EXCEL_EXTENSIONS
COMMON_US_ETF_SYMBOLS = {
    "ARKK",
    "DIA",
    "EEM",
    "GLD",
    "IWM",
    "QQQ",
    "SCHD",
    "SLV",
    "SPY",
    "TLT",
    "VIG",
    "VOO",
    "VT",
    "VTI",
    "VXUS",
    "XLE",
    "XLF",
    "XLK",
}
CRYPTO_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "USDT": "tether",
    "USDC": "usd-coin",
}
HEADER_ALIASES = {
    "symbol": {
        "symbol",
        "ticker",
        "code",
        "stockcode",
        "security",
        "securities",
        "isin",
        "stock",
        "stockid",
        "securityid",
        "productid",
        "instrument",
        "\u4ee3\u865f",
        "\u4ee3\u78bc",
        "\u80a1\u7968",
        "\u80a1\u7968\u4ee3\u865f",
        "\u80a1\u7968\u4ee3\u78bc",
        "\u80a1\u865f",
        "\u8b49\u5238\u4ee3\u865f",
        "\u8b49\u5238\u4ee3\u78bc",
        "\u5546\u54c1\u4ee3\u865f",
        "\u5546\u54c1\u4ee3\u78bc",
        "\u6a19\u7684",
        "\u6a19\u7684\u4ee3\u865f",
        "\u6a19\u7684\u4ee3\u78bc",
    },
    "name": {
        "name",
        "securityname",
        "stockname",
        "productname",
        "instrumentname",
        "\u540d\u7a31",
        "\u80a1\u540d",
        "\u8b49\u5238\u540d\u7a31",
        "\u80a1\u7968\u540d\u7a31",
        "\u5546\u54c1\u540d\u7a31",
        "\u6a19\u7684\u540d\u7a31",
    },
    "market": {
        "market",
        "exchange",
        "region",
        "\u5e02\u5834",
        "\u5e02\u5834\u5225",
        "\u4ea4\u6613\u6240",
        "\u4ea4\u6613\u5e02\u5834",
        "\u5340\u57df",
    },
    "asset_type": {
        "type",
        "assettype",
        "category",
        "assetclass",
        "\u985e\u578b",
        "\u5546\u54c1\u985e\u578b",
        "\u8cc7\u7522\u985e\u5225",
        "\u8cc7\u7522\u985e\u578b",
    },
    "quantity": {
        "quantity",
        "qty",
        "shares",
        "units",
        "position",
        "holding",
        "balance",
        "availablequantity",
        "\u80a1\u6578",
        "\u5f35\u6578",
        "\u55ae\u4f4d",
        "\u5eab\u5b58",
        "\u5eab\u5b58\u6578\u91cf",
        "\u5eab\u5b58\u80a1\u6578",
        "\u6301\u80a1",
        "\u6301\u6709\u80a1\u6578",
        "\u6301\u6709\u6578\u91cf",
        "\u6578\u91cf",
    },
    "average_cost": {
        "averagecost",
        "avgcost",
        "averageprice",
        "avgprice",
        "cost",
        "costbasisprice",
        "price",
        "unitcost",
        "\u6210\u672c",
        "\u6210\u672c\u50f9",
        "\u55ae\u4f4d\u6210\u672c",
        "\u8cb7\u9032\u6210\u672c",
        "\u5e73\u5747\u6210\u672c",
        "\u5e73\u5747\u6210\u672c\u50f9",
        "\u5e73\u5747\u50f9",
        "\u5e73\u5747\u8cb7\u9032\u6210\u672c",
        "\u6210\u4ea4\u5747\u50f9",
        "\u8cb7\u9032\u5747\u50f9",
        "\u5747\u50f9",
    },
    "currency": {"currency", "ccy", "\u5e63\u5225", "\u4ea4\u6613\u5e63\u5225", "\u8ca8\u5e63"},
    "principal_amount": {
        "principal",
        "principalamount",
        "costamount",
        "\u672c\u91d1",
        "\u6295\u5165\u672c\u91d1",
        "\u6295\u5165\u91d1\u984d",
        "\u6210\u672c\u91d1\u984d",
    },
    "principal_currency": {
        "principalcurrency",
        "principalccy",
        "\u672c\u91d1\u5e63\u5225",
        "\u6210\u672c\u5e63\u5225",
    },
    "principal_twd": {
        "principaltwd",
        "\u672c\u91d1twd",
        "\u53f0\u5e63\u672c\u91d1",
        "\u65b0\u53f0\u5e63\u672c\u91d1",
    },
}
XLSX_MAPPING_FIELDS = tuple(HEADER_ALIASES)
XLSX_REQUIRED_MAPPING_FIELDS = frozenset({"symbol", "quantity"})
MARKET_ALIASES = {
    "US": {"us", "usa", "nyse", "nasdaq", "amex", "\u7f8e\u80a1", "\u7f8e\u570b"},
    "TW": {
        "tw",
        "tpe",
        "twse",
        "tpex",
        "taiwan",
        "\u53f0\u80a1",
        "\u81fa\u80a1",
        "\u53f0\u7063",
    },
    "HK": {"hk", "hkg", "hkex", "hongkong", "\u6e2f\u80a1", "\u9999\u6e2f"},
    "CRYPTO": {
        "crypto",
        "coin",
        "\u52a0\u5bc6",
        "\u865b\u64ec\u8ca8\u5e63",
        "\u52a0\u5bc6\u8ca8\u5e63",
    },
    "FUND": {"fund", "mutualfund", "\u57fa\u91d1"},
    "INDEX": {"index", "indice", "indices", "\u6307\u6578"},
}
MARKET_SESSIONS = {
    "US": {
        "timezone": "America/New_York",
        "sessions": [(local_time(9, 30), local_time(16, 0))],
    },
    "TW": {
        "timezone": "Asia/Taipei",
        "sessions": [(local_time(9, 0), local_time(13, 30))],
    },
    "HK": {
        "timezone": "Asia/Hong_Kong",
        "sessions": [
            (local_time(9, 30), local_time(12, 0)),
            (local_time(13, 0), local_time(16, 0)),
        ],
    },
}



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


def emit_progress(enabled: bool, phase: str, message: str, **payload: Any) -> None:
    if not enabled:
        return
    event = {
        "phase": phase,
        "message": message,
        "timestamp": utc_now_text(),
        **payload,
    }
    print(f"{PROGRESS_JSON_PREFIX}{json.dumps(event, ensure_ascii=False)}", flush=True)


def run_manager(
    *,
    portfolio_file: Path,
    provider_order: list[str],
    watch: bool,
    interval_seconds: int,
    max_cycles: int,
    progress_jsonl: bool,
) -> dict[str, Any]:
    started_at = utc_now_text()
    holdings = load_portfolio(portfolio_file)
    providers = provider_registry()
    snapshots: list[dict[str, Any]] = []
    stop_reason = "completed"
    cycle = 0
    emit_progress(
        progress_jsonl,
        "portfolio_loaded",
        "Portfolio loaded",
        portfolio_file=str(portfolio_file),
        holding_count=len(holdings),
    )

    while True:
        cycle += 1
        emit_progress(
            progress_jsonl,
            "quote_cycle_started",
            f"Quote cycle {cycle} started",
            cycle=cycle,
            holding_count=len(holdings),
        )
        snapshot = create_snapshot(holdings, providers, provider_order)
        snapshots.append(snapshot)
        emit_progress(
            progress_jsonl,
            "quote_snapshot",
            f"Quote cycle {cycle} completed",
            cycle=cycle,
            snapshot=snapshot,
        )

        if not watch:
            break
        if max_cycles > 0 and cycle >= max_cycles:
            stop_reason = "max_cycles_reached"
            break
        market_summary = snapshot.get("market_summary", {})
        if market_summary.get("all_watchable_markets_closed"):
            stop_reason = "market_closed"
            emit_progress(
                progress_jsonl,
                "market_closed",
                "All watchable markets are closed",
                cycle=cycle,
                market_summary=market_summary,
            )
            break
        time.sleep(max(1, interval_seconds))

    latest_snapshot = snapshots[-1] if snapshots else None
    return {
        "ok": True,
        "tool": "ai-assistant/investment-watch",
        "action": "watch" if watch else "snapshot",
        "portfolio_file": str(portfolio_file),
        "started_at": started_at,
        "finished_at": utc_now_text(),
        "provider_order": provider_order,
        "watch": watch,
        "interval_seconds": interval_seconds,
        "cycle_count": len(snapshots),
        "stop_reason": stop_reason,
        "holding_count": len(holdings),
        "latest_snapshot": latest_snapshot,
        "snapshots": snapshots,
        "disclaimer": (
            "Quotes are for monitoring only and may be delayed. "
            "This tool does not provide investment advice."
        ),
    }


def sample_template() -> str:
    return "\n".join(
        [
            "symbol,name,market,quantity,average_cost,currency",
            "AAPL,Apple,US,10,190,USD",
            "2330,TSMC,TW,1000,600,TWD",
            "0700,Tencent,HK,100,300,HKD",
            "BTC,Bitcoin,CRYPTO,0.1,50000,USD",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investment manager")
    parser.add_argument("--portfolio", help="Portfolio CSV/TSV/JSON file")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="Emit progress JSON lines for the UI",
    )
    parser.add_argument("--watch", action="store_true", help="Poll until markets close")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Maximum watch cycles; 0 means stop only on market close or cancellation",
    )
    parser.add_argument(
        "--providers",
        default=",".join(DEFAULT_PROVIDER_ORDER),
        help="Comma-separated quote provider order",
    )
    parser.add_argument(
        "--template",
        action="store_true",
        help="Print a CSV template",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.template:
        print(sample_template())
        return 0
    if not args.portfolio:
        parser.error("--portfolio is required unless --template is used")

    provider_order = [
        item.strip()
        for item in str(args.providers).split(",")
        if item.strip()
    ] or list(DEFAULT_PROVIDER_ORDER)
    try:
        report = run_manager(
            portfolio_file=Path(args.portfolio).expanduser().resolve(),
            provider_order=provider_order,
            watch=bool(args.watch),
            interval_seconds=max(1, int(args.interval)),
            max_cycles=max(0, int(args.max_cycles)),
            progress_jsonl=bool(args.progress_jsonl),
        )
    except InvestmentManagerError as exc:
        error = {"ok": False, "tool": "ai-assistant/investment-watch", "message": str(exc)}
        if args.json:
            print(json.dumps(error, ensure_ascii=False, indent=2))
        else:
            print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        latest = report.get("latest_snapshot") or {}
        print(
            "Investment manager completed: "
            f"{latest.get('quoted_count', 0)}/{latest.get('holding_count', 0)} quoted"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ['PROGRESS_JSON_PREFIX', 'DEFAULT_INTERVAL_SECONDS', 'DEFAULT_REQUEST_TIMEOUT_SECONDS', 'DEFAULT_PROVIDER_ORDER', 'DEFAULT_IMPORT_SNAPSHOT_KEEP', 'SNAPSHOT_COPY_ATTEMPTS', 'SNAPSHOT_COPY_CHUNK_BYTES', 'XLSX_INFERRED_HEADER_SCAN_ROWS', 'XLSX_HEADER_SCAN_MAX_ROWS', 'XLSX_HEADER_SCAN_MAX_COLUMNS', 'XLSX_HORIZONTAL_GROUP_WIDTH', 'CSV_EXTENSIONS', 'JSON_EXTENSIONS', 'XLSX_EXTENSIONS', 'LEGACY_EXCEL_EXTENSIONS', 'EXCEL_EXTENSIONS', 'COMMON_US_ETF_SYMBOLS', 'CRYPTO_ID_MAP', 'HEADER_ALIASES', 'XLSX_MAPPING_FIELDS', 'XLSX_REQUIRED_MAPPING_FIELDS', 'MARKET_ALIASES', 'MARKET_SESSIONS', 'request_text', 'request_json', 'parse_unix_timestamp', 'market_status', 'yahoo_symbol', 'QuoteProvider', 'YahooChartProvider', 'YahooQuoteProvider', 'CoinGeckoProvider', 'AlphaVantageProvider', 'TwseProvider', 'provider_registry', 'provider_order_for_holding', 'quote_holding', 'quote_holding_candidates', 'quote_to_dict', 'round_number', 'holding_to_report', 'totals_by_currency', 'markets_summary', 'create_snapshot', 'emit_progress', 'run_manager', 'sample_template', 'build_parser', 'main']
