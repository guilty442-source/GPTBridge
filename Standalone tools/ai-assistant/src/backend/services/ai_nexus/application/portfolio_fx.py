from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..infrastructure.analytics_repository import number


FACTOR_PROXIES = {
    "market": ("SPY", None),
    "size": ("IWM", "SPY"),
    "value": ("IWD", "IWF"),
    "momentum": ("MTUM", "SPY"),
    "quality": ("QUAL", "SPY"),
}

HUANAN_EXCHANGE_RATE_URL = (
    "https://lovebank.hncb.com.tw/abank/pages/jsp/ExtSel/"
    "Accessibility_exchange_rate.html"
)
HUANAN_CURRENCY_NAMES = {
    "USD": "美金",
    "HKD": "港幣",
    "GBP": "英鎊",
    "NZD": "紐西蘭幣",
    "AUD": "澳幣",
    "SGD": "新加坡幣",
    "CHF": "瑞士法郎",
    "CAD": "加幣",
    "JPY": "日幣",
    "EUR": "歐元",
    "SEK": "瑞典幣",
    "ZAR": "南非幣",
    "CNY": "人民幣",
}


def sync_fx_from_huanan_bank(
    engine: "InvestmentV3Engine",
    currencies: Sequence[str],
    base_currency: str = "TWD",
    *,
    fetch_text: Any | None = None,
) -> dict[str, Any]:
    target = str(base_currency or "TWD").upper()
    if target != "TWD":
        return {
            "fx_rates_added": 0,
            "fx_errors": [
                {
                    "currency": target,
                    "message": "華南銀行牌告匯率目前以 TWD 為換算基準。",
                }
            ],
            "base_currency": target,
            "fx_provider": "Hua Nan Commercial Bank",
        }

    def default_fetch(url: str) -> str:
        raise PermissionError(
            "AI investment manager has no network access; Xingcheng must provide exchange-rate data."
        )

    html = (fetch_text or default_fetch)(HUANAN_EXCHANGE_RATE_URL)
    observed_match = re.search(r"資料生效時間\s*[：:]\s*([0-9/]+\s+[0-9:]+)", html)
    observed_at = datetime.now(timezone(timedelta(hours=8)))
    if observed_match:
        try:
            observed_at = datetime.strptime(
                observed_match.group(1), "%Y/%m/%d %H:%M:%S"
            ).replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            pass
    rows = {
        re.sub(r"<[^>]+>", "", name).strip(): (float(buy), float(sell))
        for name, buy, sell in re.findall(
            r'<td[^>]*class="first"[^>]*>(.*?)</td>\s*'
            r'<td[^>]*class="textR"[^>]*>\s*([0-9.]+)\s*</td>\s*'
            r'<td[^>]*class="textR"[^>]*>\s*([0-9.]+)\s*</td>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    requested = sorted(
        {
            str(currency or "").upper()
            for currency in currencies
            if str(currency or "").strip() and str(currency or "").upper() != "TWD"
        }
    )
    rates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    details: list[dict[str, Any]] = []
    for currency in requested:
        bank_name = HUANAN_CURRENCY_NAMES.get(currency)
        quote = rows.get(bank_name or "")
        if bank_name is None or quote is None:
            errors.append(
                {"currency": currency, "message": "華南銀行牌告匯率未提供此幣別。"}
            )
            continue
        buy, sell = quote
        midpoint = (buy + sell) / 2.0
        rates.append(
            {
                "base_currency": currency,
                "quote_currency": "TWD",
                "observed_at": observed_at.isoformat(),
                "rate": midpoint,
                "provider": "huanan-bank-spot-mid",
                "verified": True,
            }
        )
        details.append(
            {
                "currency": currency,
                "bank_name": bank_name,
                "buy_rate": buy,
                "sell_rate": sell,
                "valuation_rate": midpoint,
            }
        )
    return {
        "fx_rates_added": engine.add_fx_rates(rates),
        "fx_errors": errors,
        "base_currency": "TWD",
        "fx_provider": "Hua Nan Commercial Bank",
        "fx_rate_type": "spot_midpoint",
        "fx_observed_at": observed_at.isoformat(),
        "fx_source_url": HUANAN_EXCHANGE_RATE_URL,
        "fx_rates": details,
    }


def sync_fx_from_yahoo(
    engine: "InvestmentV3Engine",
    currencies: Sequence[str],
    base_currency: str,
    *,
    period: str = "2y",
    fetch_json: Any | None = None,
) -> dict[str, Any]:
    def default_fetch(url: str) -> dict[str, Any]:
        raise PermissionError(
            "AI investment manager has no network access; Xingcheng must provide FX history."
        )

    fetch = fetch_json or default_fetch
    target = str(base_currency or "TWD").upper()
    added = 0
    errors = []
    for source in sorted({str(value or "").upper() for value in currencies if str(value or "").strip()}):
        if source == target:
            continue
        pair = f"{source}{target}=X"
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(pair, safe="")
                + "?"
                + urllib.parse.urlencode({"range": period, "interval": "1d"})
            )
            result = fetch(url).get("chart", {}).get("result", [])[0]
            timestamps = result.get("timestamp") or []
            closes = ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or [])
            rates = [
                {
                    "base_currency": source,
                    "quote_currency": target,
                    "observed_at": datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(),
                    "rate": closes[index],
                    "provider": "yahoo-fx-history",
                    "verified": True,
                }
                for index, timestamp in enumerate(timestamps)
                if index < len(closes) and number(closes[index]) > 0
            ]
            added += engine.add_fx_rates(rates)
        except Exception as exc:
            errors.append({"currency": source, "message": str(exc)})
    return {"fx_rates_added": added, "fx_errors": errors, "base_currency": target}


def sync_factor_proxies_from_yahoo(
    engine: "InvestmentV3Engine",
    *,
    period: str = "2y",
    fetch_json: Any | None = None,
) -> dict[str, Any]:
    def default_fetch(url: str) -> dict[str, Any]:
        raise PermissionError(
            "AI investment manager has no network access; Xingcheng must provide factor data."
        )

    fetch = fetch_json or default_fetch
    added = 0
    errors = []
    symbols = sorted({symbol for pair in FACTOR_PROXIES.values() for symbol in pair if symbol})
    for symbol in symbols:
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(symbol, safe="")
                + "?"
                + urllib.parse.urlencode({"range": period, "interval": "1d"})
            )
            result = fetch(url).get("chart", {}).get("result", [])[0]
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            bars = [
                {
                    "symbol": symbol,
                    "observed_at": datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(),
                    "close": closes[index],
                    "currency": "USD",
                    "provider": "yahoo-factor-history",
                    "verified": True,
                }
                for index, timestamp in enumerate(timestamps)
                if index < len(closes) and number(closes[index]) > 0
            ]
            added += engine.store.add_price_bars(bars)
        except Exception as exc:
            errors.append({"symbol": symbol, "message": str(exc)})
    return {"factor_prices_added": added, "factor_errors": errors, "factor_symbols": symbols}
