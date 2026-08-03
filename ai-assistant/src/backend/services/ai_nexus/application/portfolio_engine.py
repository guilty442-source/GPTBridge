from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from ..infrastructure.analytics_repository import (
    TRADING_DAYS,
    InvestmentAnalyticsStore,
    _correlation,
    _covariance,
    _mean,
    _percentile,
    _returns,
    _variance,
    number,
    parse_datetime,
    rounded,
    utc_now,
    utc_text,
)
from ..infrastructure.privacy import protect_text, unprotect_text


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


def _json_hash(value: Any) -> str:
    import json

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project_weights(
    raw: dict[str, float],
    *,
    max_weight: float,
    min_weight: float = 0.0,
) -> dict[str, float]:
    symbols = list(raw)
    if not symbols:
        return {}
    cap = max(1 / len(symbols), min(1.0, max(0.01, max_weight)))
    floor = max(0.0, min(cap, min_weight))
    weights = {symbol: max(floor, number(raw[symbol])) for symbol in symbols}
    for _ in range(40):
        total = sum(weights.values())
        if total <= 0:
            weights = {symbol: 1 / len(symbols) for symbol in symbols}
        else:
            weights = {symbol: value / total for symbol, value in weights.items()}
        excess = sum(max(0.0, value - cap) for value in weights.values())
        weights = {symbol: min(cap, value) for symbol, value in weights.items()}
        if excess <= 1e-10:
            break
        available = [symbol for symbol, value in weights.items() if value < cap - 1e-10]
        if not available:
            break
        room = sum(cap - weights[symbol] for symbol in available)
        for symbol in available:
            weights[symbol] += excess * (cap - weights[symbol]) / room if room > 0 else 0
    total = sum(weights.values())
    return {symbol: value / total for symbol, value in weights.items()} if total > 0 else weights


def _portfolio_stats(
    weights: dict[str, float],
    means: dict[str, float],
    covariance: dict[tuple[str, str], float],
    samples: dict[str, list[float]],
) -> dict[str, float | None]:
    expected_daily = sum(weights.get(symbol, 0) * means.get(symbol, 0) for symbol in weights)
    variance = sum(
        weights.get(left, 0) * weights.get(right, 0) * covariance.get((left, right), 0)
        for left in weights
        for right in weights
    )
    volatility = math.sqrt(max(0.0, variance))
    common_count = min((len(samples.get(symbol, [])) for symbol in weights), default=0)
    portfolio_returns = [
        sum(weights[symbol] * samples[symbol][-common_count + index] for symbol in weights)
        for index in range(common_count)
    ] if common_count else []
    cutoff = _percentile(portfolio_returns, 0.05)
    tail = [value for value in portfolio_returns if cutoff is not None and value <= cutoff]
    annual_return = expected_daily * TRADING_DAYS
    annual_volatility = volatility * math.sqrt(TRADING_DAYS)
    return {
        "expected_return_percent": rounded(annual_return * 100, 2),
        "volatility_percent": rounded(annual_volatility * 100, 2),
        "sharpe_ratio": rounded(annual_return / annual_volatility, 4) if annual_volatility > 0 else None,
        "cvar_95_percent": rounded(max(0.0, -_mean(tail)) * 100, 2) if tail else None,
    }


def _solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    count = len(vector)
    augmented = [list(matrix[index]) + [vector[index]] for index in range(count)]
    for pivot in range(count):
        best = max(range(pivot, count), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) < 1e-12:
            return None
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        scale = augmented[pivot][pivot]
        augmented[pivot] = [value / scale for value in augmented[pivot]]
        for row in range(count):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            augmented[row] = [
                augmented[row][column] - factor * augmented[pivot][column]
                for column in range(count + 1)
            ]
    return [augmented[index][-1] for index in range(count)]


class InvestmentV3Engine:
    def __init__(self, store: InvestmentAnalyticsStore) -> None:
        self.store = store

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

    def _returns_dataset(self, symbols: Sequence[str], limit: int = 800) -> dict[str, Any]:
        returns_by_date: dict[str, dict[str, float]] = {}
        requested = list(dict.fromkeys(str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()))
        for symbol in requested:
            prices_by_date: dict[str, tuple[float, bool]] = {}
            for item in self.store.price_series(symbol, limit):
                close = number(item.get("close"))
                date = str(item.get("observed_at") or "")[:10]
                verified = bool(item.get("verified"))
                previous = prices_by_date.get(date)
                if date and close > 0 and (previous is None or verified and not previous[1]):
                    prices_by_date[date] = (close, verified)
            dates = sorted(prices_by_date)
            dated_returns = {
                current: prices_by_date[current][0] / prices_by_date[previous][0] - 1
                for previous, current in zip(dates, dates[1:])
                if prices_by_date[previous][0] > 0
            }
            if len(dated_returns) >= 20:
                returns_by_date[str(symbol)] = dated_returns
        eligible_symbols = list(returns_by_date)
        common_dates = (
            sorted(
                set.intersection(
                    *(set(returns_by_date[symbol]) for symbol in eligible_symbols)
                )
            )
            if eligible_symbols
            else []
        )
        returns = {
            symbol: [returns_by_date[symbol][date] for date in common_dates]
            for symbol in eligible_symbols
        }
        symbols = list(returns)
        means = {symbol: _mean(values) for symbol, values in returns.items()}
        covariance = {
            (left, right): _covariance(returns[left], returns[right])
            for left in symbols
            for right in symbols
        }
        return {
            "requested_symbols": requested,
            "symbols": symbols,
            "excluded_symbols": sorted(set(requested) - set(symbols)),
            "returns": returns,
            "means": means,
            "covariance": covariance,
            "sample_count": len(common_dates),
            "common_dates": common_dates,
        }

    def _analysis_positions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        positions = self.store.current_positions(state)
        return sorted(positions, key=lambda item: number(item.get("market_value")), reverse=True)

    @staticmethod
    def _analysis_coverage(
        positions: Sequence[dict[str, Any]],
        analyzed_symbols: Sequence[str],
        dataset: dict[str, Any],
    ) -> dict[str, Any]:
        analyzed = set(analyzed_symbols)
        total_value = sum(number(item.get("market_value")) for item in positions)
        analyzed_value = sum(
            number(item.get("market_value"))
            for item in positions
            if str(item.get("symbol") or "") in analyzed
        )
        requested_symbols = [
            str(item.get("symbol") or "")
            for item in positions
            if str(item.get("symbol") or "")
        ]
        return {
            "position_count": len(positions),
            "requested_symbol_count": len(set(requested_symbols)),
            "analyzed_symbol_count": len(analyzed),
            "analyzed_symbols": sorted(analyzed),
            "excluded_symbols": sorted(set(requested_symbols) - analyzed),
            "market_value_percent": (
                rounded(analyzed_value / total_value * 100, 2)
                if total_value > 0
                else None
            ),
            "common_date_count": int(dataset.get("sample_count") or 0),
            "date_alignment": "intersection_of_observed_trading_dates",
            "position_cap_applied": False,
        }

    @staticmethod
    def _normalized_position_weights(positions: Sequence[dict[str, Any]], symbols: Sequence[str]) -> dict[str, float]:
        raw = {
            str(item.get("symbol") or ""): max(0.0, number(item.get("weight_percent")) / 100)
            for item in positions
            if str(item.get("symbol") or "") in symbols
        }
        total = sum(raw.values())
        return {symbol: raw.get(symbol, 0) / total for symbol in symbols} if total > 0 else {symbol: 1 / len(symbols) for symbol in symbols}

    def optimize_portfolio(
        self,
        state: dict[str, Any],
        *,
        method: str = "risk_parity",
        max_position_percent: float = 35,
        max_turnover_percent: float = 40,
        views: dict[str, float] | None = None,
        seed: int = 73021,
    ) -> dict[str, Any]:
        positions = self._analysis_positions(state)
        dataset = self._returns_dataset([str(item.get("symbol") or "") for item in positions])
        symbols = dataset["symbols"]
        coverage = self._analysis_coverage(positions, symbols, dataset)
        if len(symbols) < 2 or dataset["sample_count"] < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": dataset["sample_count"],
                "weights": {},
                "analysis_coverage": coverage,
            }
        means: dict[str, float] = dict(dataset["means"])
        covariance: dict[tuple[str, str], float] = dict(dataset["covariance"])
        samples: dict[str, list[float]] = dict(dataset["returns"])
        current = self._normalized_position_weights(positions, symbols)
        cap = max_position_percent / 100
        if cap * len(symbols) < 1 - 1e-9:
            return {
                "ok": False,
                "status": "infeasible_constraints",
                "sample_count": dataset["sample_count"],
                "weights": {},
                "message": f"單一部位上限至少需為 {100 / len(symbols):.2f}%",
                "analysis_coverage": coverage,
            }
        method = str(method or "risk_parity")
        if method == "risk_parity":
            raw = {symbol: 1 / max(1e-8, math.sqrt(_variance(samples[symbol]))) for symbol in symbols}
        elif method in {"minimum_variance", "max_sharpe", "black_litterman"}:
            if method == "black_litterman":
                for symbol, annual_view in (views or {}).items():
                    if symbol in means:
                        means[symbol] = means[symbol] * 0.65 + number(annual_view) / 100 / TRADING_DAYS * 0.35
            raw = dict(current)
            learning_rate = 0.12
            for _ in range(500):
                portfolio_mean = sum(raw[symbol] * means[symbol] for symbol in symbols)
                portfolio_variance = sum(raw[left] * raw[right] * covariance[(left, right)] for left in symbols for right in symbols)
                portfolio_volatility = math.sqrt(max(1e-12, portfolio_variance))
                gradient = {}
                for symbol in symbols:
                    marginal_variance = sum(raw[other] * covariance[(symbol, other)] for other in symbols)
                    if method == "minimum_variance":
                        gradient[symbol] = 2 * marginal_variance
                    else:
                        gradient[symbol] = -(
                            means[symbol] * portfolio_volatility
                            - portfolio_mean * marginal_variance / portfolio_volatility
                        ) / max(1e-12, portfolio_variance)
                raw = _project_weights(
                    {symbol: raw[symbol] - learning_rate * gradient[symbol] for symbol in symbols},
                    max_weight=cap,
                )
                learning_rate *= 0.995
        elif method == "cvar":
            rng = random.Random(seed)
            candidates = []
            for _ in range(3500):
                draws = {symbol: rng.expovariate(1.0) for symbol in symbols}
                weights = _project_weights(draws, max_weight=cap)
                stats = _portfolio_stats(weights, means, covariance, samples)
                candidates.append((number(stats.get("cvar_95_percent"), 999), weights, stats))
            _score, raw, _stats = min(candidates, key=lambda item: item[0])
        else:
            raise ValueError("unsupported optimization method")
        optimized = _project_weights(raw, max_weight=cap)
        turnover = sum(abs(optimized.get(symbol, 0) - current.get(symbol, 0)) for symbol in symbols) / 2
        turnover_cap = max(0.0, max_turnover_percent / 100)
        if turnover > turnover_cap and turnover > 0:
            blend = turnover_cap / turnover
            optimized = _project_weights(
                {
                    symbol: current.get(symbol, 0) + (optimized.get(symbol, 0) - current.get(symbol, 0)) * blend
                    for symbol in symbols
                },
                max_weight=cap,
            )
            turnover = sum(abs(optimized.get(symbol, 0) - current.get(symbol, 0)) for symbol in symbols) / 2
        stats = _portfolio_stats(optimized, means, covariance, samples)
        return {
            "ok": True,
            "status": "draft",
            "method": method,
            "sample_count": dataset["sample_count"],
            "weights": {symbol: rounded(value * 100, 2) for symbol, value in optimized.items()},
            "current_weights": {symbol: rounded(value * 100, 2) for symbol, value in current.items()},
            "turnover_percent": rounded(turnover * 100, 2),
            "max_position_percent": max_position_percent,
            "max_turnover_percent": max_turnover_percent,
            "statistics": stats,
            "execution_policy": "simulation_only_human_approval_required",
            "assumptions": ["historical daily returns", "long only", "weights sum to 100%", "no automatic order submission"],
            "analysis_coverage": coverage,
        }

    def regime_detection(self, state: dict[str, Any]) -> dict[str, Any]:
        risk = self.store.risk(state)
        positions = self._analysis_positions(state)
        dataset = self._returns_dataset([str(item.get("symbol") or "") for item in positions], 260)
        symbols = dataset["symbols"]
        coverage = self._analysis_coverage(positions, symbols, dataset)
        if dataset["sample_count"] < 20:
            return {
                "status": "insufficient_history",
                "sample_count": dataset["sample_count"],
                "regime": "unknown",
                "analysis_coverage": coverage,
            }
        weights = self._normalized_position_weights(positions, symbols)
        portfolio = [
            sum(weights[symbol] * dataset["returns"][symbol][index] for symbol in symbols)
            for index in range(dataset["sample_count"])
        ]
        return_20 = math.prod(1 + value for value in portfolio[-20:]) - 1
        return_60 = math.prod(1 + value for value in portfolio[-60:]) - 1 if len(portfolio) >= 60 else return_20
        volatility_20 = statistics.stdev(portfolio[-20:]) * math.sqrt(TRADING_DAYS) if len(portfolio) >= 20 else 0
        if volatility_20 >= 0.30:
            regime = "high_volatility"
            label = "高波動"
        elif return_20 <= -0.08 or return_60 <= -0.15:
            regime = "bear"
            label = "空頭"
        elif return_20 >= 0.06 and return_60 > 0:
            regime = "bull"
            label = "多頭"
        elif abs(return_20) <= 0.025:
            regime = "sideways"
            label = "盤整"
        else:
            regime = "transition"
            label = "轉換期"
        return {
            "status": "ready",
            "regime": regime,
            "label": label,
            "confidence": rounded(min(0.95, 0.5 + abs(return_20) * 2 + volatility_20 * 0.4), 3),
            "return_20d_percent": rounded(return_20 * 100, 2),
            "return_60d_percent": rounded(return_60 * 100, 2),
            "volatility_20d_percent": rounded(volatility_20 * 100, 2),
            "var_95_percent": risk.get("var_95_one_day_percent"),
            "sample_count": len(portfolio),
            "analysis_coverage": coverage,
        }

    def monte_carlo(
        self,
        state: dict[str, Any],
        *,
        simulations: int = 2000,
        horizon_days: int = 252,
        target_return_percent: float = 0,
        seed: int = 73021,
    ) -> dict[str, Any]:
        positions = self._analysis_positions(state)
        dataset = self._returns_dataset([str(item.get("symbol") or "") for item in positions], 800)
        symbols = dataset["symbols"]
        coverage = self._analysis_coverage(positions, symbols, dataset)
        if dataset["sample_count"] < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": dataset["sample_count"],
                "analysis_coverage": coverage,
            }
        weights = self._normalized_position_weights(positions, symbols)
        historical = [
            sum(weights[symbol] * dataset["returns"][symbol][index] for symbol in symbols)
            for index in range(dataset["sample_count"])
        ]
        mean = _mean(historical)
        volatility = statistics.stdev(historical)
        current_value = sum(number(item.get("market_value")) for item in positions)
        rng = random.Random(seed)
        terminal_returns = []
        drawdowns = []
        count = max(200, min(20000, int(simulations)))
        days = max(5, min(2520, int(horizon_days)))
        for _ in range(count):
            value = 1.0
            peak = 1.0
            worst = 0.0
            for _day in range(days):
                shock = rng.gauss(mean, volatility)
                value *= max(0.01, 1 + shock)
                peak = max(peak, value)
                worst = min(worst, value / peak - 1)
            terminal_returns.append(value - 1)
            drawdowns.append(worst)
        ordered = sorted(terminal_returns)
        percentile = lambda level: ordered[min(len(ordered) - 1, max(0, int(level * (len(ordered) - 1))))]
        target = target_return_percent / 100
        return {
            "ok": True,
            "status": "ready",
            "simulations": count,
            "horizon_days": days,
            "sample_count": len(historical),
            "current_value": rounded(current_value, 2),
            "terminal_value": {
                "p05": rounded(current_value * (1 + percentile(0.05)), 2),
                "p50": rounded(current_value * (1 + percentile(0.50)), 2),
                "p95": rounded(current_value * (1 + percentile(0.95)), 2),
            },
            "terminal_return_percent": {
                "p05": rounded(percentile(0.05) * 100, 2),
                "p50": rounded(percentile(0.50) * 100, 2),
                "p95": rounded(percentile(0.95) * 100, 2),
            },
            "probability_of_loss_percent": rounded(sum(value < 0 for value in terminal_returns) / count * 100, 2),
            "target_success_percent": rounded(sum(value >= target for value in terminal_returns) / count * 100, 2),
            "median_max_drawdown_percent": rounded(statistics.median(drawdowns) * 100, 2),
            "methodology": "seeded univariate Gaussian fit to current-weight portfolio returns",
            "assumptions": [
                "Current position weights are held constant for the simulated horizon.",
                "Daily portfolio returns are independent Gaussian draws.",
                "Fat tails, volatility regimes, cash flows, taxes and trading costs are not modeled.",
            ],
            "analysis_coverage": coverage,
            "seed": seed,
        }

    def factor_attribution(self, state: dict[str, Any]) -> dict[str, Any]:
        positions = self._analysis_positions(state)
        portfolio_dataset = self._returns_dataset([str(item.get("symbol") or "") for item in positions], 800)
        symbols = portfolio_dataset["symbols"]
        coverage = self._analysis_coverage(positions, symbols, portfolio_dataset)
        if portfolio_dataset["sample_count"] < 30:
            return {
                "status": "insufficient_history",
                "sample_count": portfolio_dataset["sample_count"],
                "factors": [],
                "analysis_coverage": coverage,
            }
        weights = self._normalized_position_weights(positions, symbols)
        portfolio_returns = [
            sum(weights[symbol] * portfolio_dataset["returns"][symbol][index] for symbol in symbols)
            for index in range(portfolio_dataset["sample_count"])
        ]
        factor_series: dict[str, list[float]] = {}
        unavailable = []
        for factor, (long_symbol, short_symbol) in FACTOR_PROXIES.items():
            long_prices = [number(item.get("close")) for item in self.store.price_series(long_symbol, 800)]
            short_prices = [number(item.get("close")) for item in self.store.price_series(short_symbol, 800)] if short_symbol else []
            long_returns = _returns(long_prices)
            if not long_returns:
                unavailable.append(factor)
                continue
            if short_symbol:
                short_returns = _returns(short_prices)
                common = min(len(long_returns), len(short_returns), len(portfolio_returns))
                if common < 30:
                    unavailable.append(factor)
                    continue
                factor_series[factor] = [long_returns[-common + index] - short_returns[-common + index] for index in range(common)]
            else:
                factor_series[factor] = long_returns[-len(portfolio_returns):]
        if not factor_series:
            return {
                "status": "factor_proxies_missing",
                "sample_count": len(portfolio_returns),
                "factors": [],
                "unavailable_factors": unavailable,
                "analysis_coverage": coverage,
            }
        common = min(len(portfolio_returns), *(len(values) for values in factor_series.values()))
        names = list(factor_series)
        x_rows = [[1.0] + [factor_series[name][-common + index] for name in names] for index in range(common)]
        y = portfolio_returns[-common:]
        columns = len(names) + 1
        matrix = [[sum(row[left] * row[right] for row in x_rows) for right in range(columns)] for left in range(columns)]
        for index in range(columns):
            matrix[index][index] += 1e-8
        vector = [sum(row[column] * y[index] for index, row in enumerate(x_rows)) for column in range(columns)]
        coefficients = _solve_linear(matrix, vector)
        if coefficients is None:
            return {
                "status": "regression_failed",
                "sample_count": common,
                "factors": [],
                "analysis_coverage": coverage,
            }
        predicted = [sum(coefficients[column] * row[column] for column in range(columns)) for row in x_rows]
        residual = sum((y[index] - predicted[index]) ** 2 for index in range(common))
        total = sum((value - _mean(y)) ** 2 for value in y)
        factors = []
        for index, name in enumerate(names, start=1):
            contribution = coefficients[index] * _mean(factor_series[name][-common:]) * TRADING_DAYS
            factors.append(
                {
                    "factor": name,
                    "exposure": rounded(coefficients[index], 4),
                    "annual_contribution_percent": rounded(contribution * 100, 2),
                    "correlation": rounded(_correlation(y, factor_series[name][-common:]), 4),
                }
            )
        symbol_contributions = []
        for position in positions:
            symbol = str(position.get("symbol") or "")
            values = portfolio_dataset["returns"].get(symbol, [])
            contribution = weights.get(symbol, 0) * (_mean(values) * TRADING_DAYS if values else 0)
            symbol_contributions.append({"symbol": symbol, "contribution_percent": rounded(contribution * 100, 2), "weight_percent": position.get("weight_percent")})
        return {
            "status": "ready",
            "sample_count": common,
            "alpha_annual_percent": rounded(coefficients[0] * TRADING_DAYS * 100, 2),
            "r_squared": rounded(1 - residual / total, 4) if total > 0 else None,
            "factors": factors,
            "symbol_contributions": sorted(symbol_contributions, key=lambda item: abs(number(item.get("contribution_percent"))), reverse=True),
            "unavailable_factors": unavailable,
            "methodology": "ridge-stabilized proxy factor regression",
            "analysis_coverage": coverage,
        }

    def add_corporate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        action_type = str(payload.get("action_type") or payload.get("event_type") or "").strip().lower()
        effective = parse_datetime(payload.get("effective_at") or payload.get("scheduled_at")) or utc_now()
        if not symbol or action_type not in {"dividend", "split", "symbol_change", "merger", "delisting", "fund_distribution"}:
            raise ValueError("invalid corporate action")
        ratio = number(payload.get("ratio")) if payload.get("ratio") is not None else None
        cash_amount = number(payload.get("cash_amount")) if payload.get("cash_amount") is not None else None
        issues = []
        if action_type == "split" and (ratio is None or ratio <= 0):
            issues.append("拆併股比例必須大於零")
        if action_type in {"dividend", "fund_distribution"} and (cash_amount is None or cash_amount < 0):
            issues.append("現金金額不可小於零")
        dedupe_source = f"{symbol}|{action_type}|{utc_text(effective)[:10]}|{ratio}|{cash_amount}"
        row = {
            "action_id": uuid.uuid4().hex,
            "dedupe_key": hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest(),
            "symbol": symbol,
            "action_type": action_type,
            "effective_at": utc_text(effective),
            "ratio": ratio,
            "cash_amount": cash_amount,
            "currency": str(payload.get("currency") or "").upper(),
            "old_symbol": str(payload.get("old_symbol") or "").upper(),
            "new_symbol": str(payload.get("new_symbol") or "").upper(),
            "source": str(payload.get("source") or "manual"),
            "confidence": max(0.0, min(1.0, number(payload.get("confidence"), 0.5))),
            "status": "needs_correction" if issues else "pending_review",
            "details_encrypted": protect_text(json.dumps(payload.get("details") or {}, ensure_ascii=False, default=str)),
            "created_at": utc_text(),
            "reviewed_at": "",
        }
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT action_id FROM corporate_actions WHERE dedupe_key=?",
                (row["dedupe_key"],),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO corporate_actions VALUES(
                    :action_id, :dedupe_key, :symbol, :action_type, :effective_at,
                    :ratio, :cash_amount, :currency, :old_symbol, :new_symbol,
                    :source, :confidence, :status, :details_encrypted, :created_at, :reviewed_at
                ) ON CONFLICT(dedupe_key) DO UPDATE SET
                    source=excluded.source, confidence=MAX(corporate_actions.confidence, excluded.confidence)
                """,
                row,
            )
            for issue in issues if not existing else []:
                connection.execute(
                    "INSERT INTO data_quality_issues VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, utc_text(), "corporate_action_validation", symbol, "critical", "企業行動資料異常", protect_text(issue), "open", ""),
                )
            stored = connection.execute(
                "SELECT * FROM corporate_actions WHERE dedupe_key=?",
                (row["dedupe_key"],),
            ).fetchone()
        self.store.audit("corporate_action_ingested", {"symbol": symbol, "action_type": action_type, "status": row["status"], "created": not bool(existing)})
        public = dict(stored or row)
        public["details"] = unprotect_text(public.pop("details_encrypted"))
        public["issues"] = issues
        public["created"] = not bool(existing)
        return public

    def review_corporate_action(self, action_id: str, status: str) -> bool:
        normalized = status if status in {"approved", "rejected", "pending_review", "needs_correction"} else "pending_review"
        with self.store.connect() as connection:
            current = connection.execute(
                "SELECT symbol, status FROM corporate_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if current and normalized == "approved" and current["status"] == "needs_correction":
                raise ValueError("資料品質問題尚未修正，不能核准公司行動")
            cursor = connection.execute(
                "UPDATE corporate_actions SET status = ?, reviewed_at = ? WHERE action_id = ?",
                (normalized, utc_text(), action_id),
            )
            if current and normalized in {"approved", "rejected"}:
                connection.execute(
                    "UPDATE data_quality_issues SET status='resolved', resolved_at=? WHERE symbol=? AND issue_type='corporate_action_validation' AND status='open'",
                    (utc_text(), current["symbol"]),
                )
        if cursor.rowcount:
            self.store.audit("corporate_action_reviewed", {"action_id": action_id, "status": normalized})
        return bool(cursor.rowcount)

    def corporate_action_governance(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            action_rows = connection.execute("SELECT * FROM corporate_actions ORDER BY effective_at DESC LIMIT 500").fetchall()
            issue_rows = connection.execute("SELECT * FROM data_quality_issues ORDER BY created_at DESC LIMIT 500").fetchall()
        actions = []
        for row in action_rows:
            item = dict(row)
            details = unprotect_text(str(item.pop("details_encrypted", "") or ""))
            try:
                item["details"] = json.loads(details)
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = details
            actions.append(item)
        issues = []
        for row in issue_rows:
            item = dict(row)
            item["detail"] = unprotect_text(str(item.pop("detail_encrypted", "") or ""))
            issues.append(item)
        return {
            "status": "attention" if any(item.get("status") == "needs_correction" for item in actions) else "ready",
            "actions": actions,
            "issues": issues,
            "pending_count": sum(1 for item in actions if item.get("status") in {"pending_review", "needs_correction"}),
            "approved_count": sum(1 for item in actions if item.get("status") == "approved"),
            "policy": "raw transactions and holdings are never changed before human approval",
        }

    def snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": "1.0.0",
            "generated_at": utc_text(),
            "multi_currency": self.multi_currency_accounting(state),
            "regime": self.regime_detection(state),
            "factors": self.factor_attribution(state),
            "corporate_actions": self.corporate_action_governance(),
        }
