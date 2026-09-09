from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..infrastructure.analytics_repository import (
    number,
    parse_datetime,
    rounded,
    utc_now,
    utc_text,
)


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


class InvestmentEngFxMixin:
    def add_fx_rates(self, rates: Sequence[dict[str, Any]]) -> int:
        prepared = []
        for item in rates:
            base = str(item.get("base_currency") or item.get("base") or "").upper()
            quote = str(item.get("quote_currency") or item.get("quote") or "").upper()
            observed = parse_datetime(item.get("observed_at") or item.get("timestamp"))
            rate = number(item.get("rate"), -1)
            if not base or not quote or observed is None or rate <= 0:
                continue
            prepared.append(
                (
                    base,
                    quote,
                    utc_text(observed),
                    rate,
                    str(item.get("provider") or "manual"),
                    int(bool(item.get("verified", True))),
                )
            )
        if not prepared:
            return 0
        with self.store.connect() as connection:
            connection.executemany(
                """
                INSERT INTO fx_rates(base_currency, quote_currency, observed_at, rate, provider, verified)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(base_currency, quote_currency, observed_at, provider) DO UPDATE SET
                    rate=excluded.rate, verified=excluded.verified
                """,
                prepared,
            )
        self.store.audit("fx_rates_imported", {"count": len(prepared)})
        return len(prepared)

    def fx_rate(
        self,
        source_currency: str,
        target_currency: str,
        at: datetime | None = None,
    ) -> dict[str, Any] | None:
        source = str(source_currency or "").upper()
        target = str(target_currency or "").upper()
        if not source or not target:
            return None
        if source == target:
            return {"rate": 1.0, "observed_at": utc_text(at), "provider": "identity", "verified": True}
        cutoff = utc_text(at or utc_now())
        with self.store.connect() as connection:
            direct = connection.execute(
                """
                SELECT * FROM fx_rates
                WHERE base_currency = ? AND quote_currency = ? AND observed_at <= ?
                ORDER BY observed_at DESC, verified DESC LIMIT 1
                """,
                (source, target, cutoff),
            ).fetchone()
            if direct:
                return dict(direct)
            inverse = connection.execute(
                """
                SELECT * FROM fx_rates
                WHERE base_currency = ? AND quote_currency = ? AND observed_at <= ?
                ORDER BY observed_at DESC, verified DESC LIMIT 1
                """,
                (target, source, cutoff),
            ).fetchone()
        if inverse and number(inverse["rate"]) > 0:
            item = dict(inverse)
            item["rate"] = 1 / number(inverse["rate"])
            item["inverse"] = True
            return item
        return None

    def multi_currency_accounting(self, state: dict[str, Any]) -> dict[str, Any]:
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        base_currency = str(self.store.get_setting("base_currency", "TWD") or "TWD").upper()
        latest = self.store.latest_prices()
        transactions = list(reversed(self.store.list_transactions(5000)))
        acquisition_fx: dict[str, list[tuple[float, float]]] = {}
        for transaction in transactions:
            if transaction.get("side") != "BUY":
                continue
            symbol = str(transaction.get("symbol") or "")
            currency = str(transaction.get("currency") or base_currency)
            occurred = parse_datetime(transaction.get("occurred_at"))
            quote = self.fx_rate(currency, base_currency, occurred)
            if quote:
                acquisition_fx.setdefault(symbol, []).append(
                    (number(transaction.get("quantity")), number(quote.get("rate")))
                )
        rows = []
        missing: set[str] = set()
        totals = {
            "market_value": 0.0,
            "cost_value": 0.0,
            "asset_pnl": 0.0,
            "currency_pnl": 0.0,
        }
        for holding in holdings:
            symbol = str(holding.get("symbol") or "").upper()
            currency = str(holding.get("currency") or base_currency).upper()
            quantity = number(holding.get("quantity"))
            average_cost = number(holding.get("average_cost"))
            current_price = number(latest.get(symbol, {}).get("close"), average_cost)
            current_fx_quote = self.fx_rate(currency, base_currency)
            if current_fx_quote is None:
                missing.add(currency)
                rows.append({"symbol": symbol, "currency": currency, "status": "missing_fx_rate"})
                continue
            current_fx = number(current_fx_quote.get("rate"), 1)
            fx_lots = acquisition_fx.get(symbol, [])
            fx_quantity = sum(item[0] for item in fx_lots)
            cost_fx = (
                sum(quantity_value * rate for quantity_value, rate in fx_lots) / fx_quantity
                if fx_quantity > 0
                else current_fx
            )
            market_value = quantity * current_price * current_fx
            cost_value = quantity * average_cost * cost_fx
            asset_pnl = quantity * (current_price - average_cost) * cost_fx
            currency_pnl = quantity * current_price * (current_fx - cost_fx)
            totals["market_value"] += market_value
            totals["cost_value"] += cost_value
            totals["asset_pnl"] += asset_pnl
            totals["currency_pnl"] += currency_pnl
            rows.append(
                {
                    "symbol": symbol,
                    "currency": currency,
                    "quantity": rounded(quantity, 6),
                    "current_price": rounded(current_price, 6),
                    "current_fx_rate": rounded(current_fx, 8),
                    "cost_fx_rate": rounded(cost_fx, 8),
                    "market_value_base": rounded(market_value, 2),
                    "cost_value_base": rounded(cost_value, 2),
                    "asset_pnl_base": rounded(asset_pnl, 2),
                    "currency_pnl_base": rounded(currency_pnl, 2),
                    "total_pnl_base": rounded(market_value - cost_value, 2),
                    "status": "ready",
                }
            )
        return {
            "status": "ready" if rows and not missing else "incomplete_fx" if rows else "empty",
            "base_currency": base_currency,
            "market_value_base": rounded(totals["market_value"], 2),
            "cost_value_base": rounded(totals["cost_value"], 2),
            "total_pnl_base": rounded(totals["market_value"] - totals["cost_value"], 2),
            "asset_pnl_base": rounded(totals["asset_pnl"], 2),
            "currency_pnl_base": rounded(totals["currency_pnl"], 2),
            "missing_currencies": sorted(missing),
            "positions": rows,
            "methodology": "historical-acquisition-fx-and-latest-market-fx",
        }
