from __future__ import annotations

import copy
import json
import math
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Sequence

from .market_sources import market_source_catalog


FetchJson = Callable[[str, dict[str, Any] | None], dict[str, Any]]
FUNDCLEAR_BASE = "https://www.fundclear.com.tw"
FUNDCLEAR_SEARCH_URL = f"{FUNDCLEAR_BASE}/api/search/fund/query-fund"
TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"

YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
NETWORK_DESTINATION_ALLOWLIST = frozenset({
    "www.fundclear.com.tw",
    "openapi.twse.com.tw",
    "www.tpex.org.tw",
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
})
SUPPORTED_QUOTE_TYPES = {"EQUITY", "ETF", "MUTUALFUND", "INDEX"}
MARKET_SUFFIXES = {
    ".TW": "TW",
    ".TWO": "TW",
    ".HK": "HK",
    ".T": "JP",
    ".L": "UK",
    ".AX": "AU",
    ".DE": "DE",
    ".SI": "SG",
    ".TO": "CA",
    ".V": "CA",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _fetch_json(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in NETWORK_DESTINATION_ALLOWLIST:
        raise PermissionError("NETWORK_DESTINATION_DENIED")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": FUNDCLEAR_BASE if url.startswith(FUNDCLEAR_BASE) else "https://finance.yahoo.com",
            "Referer": f"{FUNDCLEAR_BASE}/fund-search" if url.startswith(FUNDCLEAR_BASE) else "https://finance.yahoo.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138 Safari/537.36",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else {"data": value}


def _normalized_name(value: str) -> str:
    text = str(value or "").upper()
    text = re.sub(r"本基金[^)）]*", "", text)
    text = re.sub(r"[（(][^)）]*[)）]", "", text)
    text = re.sub(
        r"(?:新台幣|台幣|美元|澳幣|人民幣|南非幣|日圓|歐元|港幣|避險|基金)",
        "",
        text,
    )
    return re.sub(r"[^0-9A-Z\u3400-\u9fff]+", "", text)


def _normalized_identifier(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("：", ":").replace("．", ".")
    text = re.sub(r"\s+", "", text)
    if ":" in text:
        prefix, candidate = text.split(":", 1)
        if prefix in {"TW", "TPE", "TPEX", "US", "NASDAQ", "NYSE", "HK", "JP"}:
            text = candidate
    return re.sub(r"[^0-9A-Z.\-]", "", text)


def recognize_holding_identity(holding: dict[str, Any]) -> dict[str, Any]:
    """Normalize a user holding without guessing business data."""

    original_symbol = str(
        holding.get("fund_quote_symbol") or holding.get("symbol") or ""
    ).strip().upper()
    symbol = _normalized_identifier(
        holding.get("fund_quote_symbol") or holding.get("symbol")
    )
    market = str(holding.get("market") or "").strip().upper()
    asset_type = str(holding.get("asset_type") or "").strip().upper()
    name = str(holding.get("name") or "").strip()
    isin = _normalized_identifier(holding.get("fund_isin") or holding.get("isin"))
    fund_code = _normalized_identifier(holding.get("fund_code"))
    inferred: list[str] = []
    if market in {"AUTO", "OTHER"}:
        market = ""
    if ":" in original_symbol:
        prefix = original_symbol.split(":", 1)[0]
        prefix_markets = {
            "TW": "TW",
            "TPE": "TW",
            "TPEX": "TW",
            "US": "US",
            "NASDAQ": "US",
            "NYSE": "US",
            "HK": "HK",
            "JP": "JP",
        }
        if prefix in prefix_markets:
            market = prefix_markets[prefix]
            inferred.append("market-from-symbol-prefix")
    for suffix, inferred_market in sorted(
        MARKET_SUFFIXES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if symbol.endswith(suffix):
            if not market or market in {"AUTO", "OTHER"}:
                market = inferred_market
                inferred.append("market-from-symbol-suffix")
            break
    fund_tokens = ("基金", "FUND", "級別", "SHARE CLASS")
    if (
        market == "FUND"
        or asset_type == "FUND"
        or fund_code
        or any(token in name.upper() for token in fund_tokens)
    ):
        market = "FUND"
        asset_type = "FUND"
        inferred.append("fund-from-identity-fields")
    if not market:
        if re.fullmatch(r"\d{4,6}", symbol):
            market = "TW"
            inferred.append("market-from-numeric-symbol")
        elif symbol:
            market = "US"
            inferred.append("market-from-alpha-symbol")
    if not asset_type or asset_type == "AUTO":
        asset_type = "ETF" if "ETF" in name.upper() else "STOCK"
        inferred.append("asset-type-from-name")
    confidence = 0.98 if symbol and holding.get("market") else 0.86 if symbol else 0.62 if name else 0.0
    return {
        **holding,
        "symbol": symbol,
        "market": market,
        "asset_type": asset_type,
        "name": name,
        "isin": isin,
        "fund_isin": isin,
        "fund_code": fund_code,
        "recognition": {
            "status": "recognized" if symbol or name else "insufficient",
            "confidence": confidence,
            "inferred_fields": list(dict.fromkeys(inferred)),
            "original_symbol": str(holding.get("symbol") or ""),
        },
    }


def _fund_query_terms(
    name: str,
    *,
    fund_code: str = "",
    isin: str = "",
) -> list[str]:
    cleaned = re.sub(r"[（(].*$", "", str(name or "")).strip()
    cleaned = re.sub(
        r"(?:新台幣|台幣|美元|澳幣|人民幣|南非幣|日圓|歐元|港幣)$", "", cleaned
    )
    cleaned = re.sub(r"(?:A\d*|B|C|I|S|T|AI|ADMC\d*)級?別?$", "", cleaned, flags=re.I)
    managers = (
        "摩根士丹利", "富蘭克林華美", "東方匯理", "中國信託", "台中銀",
        "路博邁", "貝萊德", "聯博", "統一", "野村", "復華", "國泰", "瀚亞",
        "安聯", "景順", "摩根", "街口", "富邦", "元大", "群益", "第一金",
    )
    manager = next((item for item in managers if cleaned.startswith(item)), cleaned[:2])
    core = cleaned[len(manager):]
    normalized_code = _normalized_identifier(fund_code)
    normalized_isin = _normalized_identifier(isin)
    core_tokens = [
        token
        for token in re.split(r"[\s\-_/]+", re.sub(r"[（(].*$", "", str(name or "")))
        if len(token) >= 2
    ]
    candidates = [
        normalized_code,
        normalized_isin,
        cleaned,
        " ".join(core_tokens[:6]),
        f"{manager}{core[:8]}",
        f"{manager}{core[:5]}",
        manager,
    ]
    result: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip(" -–—_")
        if len(candidate) >= 2 and candidate not in result:
            result.append(candidate)
    return result[:8]


def _yahoo_symbol_candidates(symbol: str, market: str) -> list[str]:
    normalized_symbol = _normalized_identifier(symbol)
    normalized_market = str(market or "").strip().upper()
    if not normalized_symbol:
        return []
    if "." in normalized_symbol:
        return [normalized_symbol]
    if normalized_market == "TW":
        return [f"{normalized_symbol}.TW", f"{normalized_symbol}.TWO"]
    suffixes = {
        "HK": ".HK",
        "JP": ".T",
        "UK": ".L",
        "AU": ".AX",
        "DE": ".DE",
        "SG": ".SI",
    }
    if normalized_market == "CA":
        return [f"{normalized_symbol}.TO", f"{normalized_symbol}.V"]
    suffix = suffixes.get(normalized_market)
    if suffix:
        if normalized_market == "HK" and normalized_symbol.isdigit():
            normalized_symbol = normalized_symbol.zfill(4)
        return [f"{normalized_symbol}{suffix}"]
    return [normalized_symbol]


def _currency_code(value: str) -> str:
    text = str(value or "").upper()
    mapping = {
        "新台幣": "TWD", "台幣": "TWD", "美元": "USD", "澳幣": "AUD",
        "歐元": "EUR", "人民幣": "CNY", "日圓": "JPY", "港幣": "HKD",
        "南非幣": "ZAR", "英鎊": "GBP", "瑞士法郎": "CHF",
    }
    return mapping.get(text, text)


def _roc_date_iso(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 7:
        return ""
    try:
        year = int(digits[:3]) + 1911
        month = int(digits[3:5])
        day = int(digits[5:7])
        return datetime(year, month, day, tzinfo=timezone(timedelta(hours=8))).isoformat()
    except ValueError:
        return ""


def _infer_frequency(events: Sequence[dict[str, Any]], declared: str = "") -> dict[str, Any]:
    declared_text = str(declared or "")
    declared_mapping = {
        "週配": ("weekly", "每週", 52), "月配": ("monthly", "每月", 12),
        "季配": ("quarterly", "每季", 4), "半年": ("semiannual", "每半年", 2),
        "年配": ("annual", "每年", 1), "不配息": ("none", "無配息", 0),
        "不分配": ("none", "無配息", 0),
    }
    for token, (code, label, count) in declared_mapping.items():
        if token in declared_text:
            return {
                "code": code,
                "label": label,
                "per_year": count,
                "confidence": 0.95,
            }
    dates: list[datetime] = []
    for event in events:
        value = str(event.get("observed_at") or event.get("record_date") or "")
        try:
            dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            continue
    dates = sorted(set(dates))
    if len(dates) < 2:
        return {"code": "unknown", "label": "待累積資料", "per_year": None, "confidence": 0.0}
    intervals = [(dates[index] - dates[index - 1]).days for index in range(1, len(dates))]
    median = statistics.median(intervals)
    definitions = ((10, "weekly", "每週", 52), (45, "monthly", "每月", 12), (120, "quarterly", "每季", 4), (220, "semiannual", "每半年", 2), (420, "annual", "每年", 1))
    for maximum, code, label, count in definitions:
        if median <= maximum:
            return {"code": code, "label": label, "per_year": count, "median_days": median, "confidence": min(0.95, 0.55 + len(intervals) * 0.08)}
    return {"code": "irregular", "label": "不定期", "per_year": None, "median_days": median, "confidence": 0.55}


class MarketDataSearch:
    MAX_HOLDING_CACHE_ENTRIES = 32
    """星澄的唯讀市場搜尋層；不讀取投資管家資料庫。"""

    def __init__(self, fetch_json: FetchJson | None = None) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self._holding_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._holding_cache_lock = threading.Lock()
        self._dataset_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._dataset_cache_lock = threading.Lock()

    @staticmethod
    def _holding_cache_key(holding: dict[str, Any]) -> str:
        return json.dumps(
            {
                key: holding.get(key)
                for key in (
                    "symbol",
                    "name",
                    "market",
                    "asset_type",
                    "currency",
                    "fund_quote_symbol",
                    "fund_code",
                    "fund_isin",
                    "isin",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _holding_cache_ttl(result: dict[str, Any]) -> int:
        distribution = result.get("distribution")
        if isinstance(distribution, dict) and distribution.get("frequency") == "none":
            return 86400
        if str(result.get("asset_type") or "").upper() == "FUND":
            return 900
        return 60

    def release_idle_resources(self, now: float | None = None) -> None:
        checked_at = time.monotonic() if now is None else float(now)
        with self._holding_cache_lock:
            self._holding_cache = {
                key: value
                for key, value in self._holding_cache.items()
                if value[0] > checked_at
            }
        with self._dataset_cache_lock:
            self._dataset_cache = {
                key: value
                for key, value in self._dataset_cache.items()
                if value[0] > checked_at
            }

    def _cached_dataset(self, url: str, ttl_seconds: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._dataset_cache_lock:
            cached = self._dataset_cache.get(url)
            if cached and cached[0] > now:
                return copy.deepcopy(cached[1])
            payload = self.fetch_json(url, None)
            rows = payload.get("data") if isinstance(payload, dict) else []
            dataset = [dict(item) for item in rows if isinstance(item, dict)]
            self._dataset_cache[url] = (now + max(30, ttl_seconds), dataset)
            return copy.deepcopy(dataset)

    def _search_holding_cached(
        self, holding: dict[str, Any], *, force_refresh: bool = False
    ) -> dict[str, Any]:
        key = self._holding_cache_key(holding)
        now = time.monotonic()
        with self._holding_cache_lock:
            cached = self._holding_cache.get(key)
            if cached and cached[0] > now and not force_refresh:
                result = copy.deepcopy(cached[1])
                result["cache"] = {
                    "hit": True,
                    "ttl_policy": "no-distribution-daily"
                    if self._holding_cache_ttl(result) == 86400
                    else "market-data",
                }
                return result
            if cached and cached[0] <= now:
                self._holding_cache.pop(key, None)
        result = self._search_holding(holding)
        if result.get("ok"):
            ttl = self._holding_cache_ttl(result)
            with self._holding_cache_lock:
                self._holding_cache[key] = (now + ttl, copy.deepcopy(result))
                expired = [
                    cache_key
                    for cache_key, (until, _cached) in self._holding_cache.items()
                    if until <= now
                ]
                for cache_key in expired:
                    self._holding_cache.pop(cache_key, None)
                while len(self._holding_cache) > self.MAX_HOLDING_CACHE_ENTRIES:
                    self._holding_cache.pop(next(iter(self._holding_cache)))
            result["cache"] = {
                "hit": False,
                "ttl_seconds": ttl,
                "ttl_policy": "no-distribution-daily" if ttl == 86400 else "market-data",
            }
        return result

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("holdings")
        if not isinstance(requested, list):
            requested = [payload.get("holding") or payload]
        holdings = [
            recognize_holding_identity(dict(item))
            for item in requested
            if isinstance(item, dict)
        ][:300]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        workers = max(1, min(int(payload.get("max_workers") or 4), 6))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._search_holding_cached,
                    holding,
                    force_refresh=payload.get("force_refresh") is True,
                ): holding
                for holding in holdings
            }
            for future in as_completed(futures):
                holding = futures[future]
                try:
                    result = future.result()
                    if result.get("ok"):
                        results.append(result)
                    else:
                        errors.append(
                            {
                                "symbol": str(holding.get("symbol") or ""),
                                "name": str(holding.get("name") or ""),
                                "market": str(holding.get("market") or ""),
                                "asset_type": str(holding.get("asset_type") or ""),
                                "message": str(result.get("message") or "找不到可驗證資料"),
                                "recognition_status": str(
                                    (holding.get("recognition") or {}).get("status") or ""
                                ),
                            }
                        )
                except Exception as error:
                    errors.append(
                        {
                            "symbol": str(holding.get("symbol") or ""),
                            "name": str(holding.get("name") or ""),
                            "market": str(holding.get("market") or ""),
                            "asset_type": str(holding.get("asset_type") or ""),
                            "message": f"{type(error).__name__}: {error}",
                            "recognition_status": "failed",
                        }
                    )
        results.sort(key=lambda item: (str(item.get("market")), str(item.get("requested_symbol"))))
        return {
            "ok": bool(results) and not (payload.get("require_all") and errors),
            "provider": "星澄即時網路搜尋",
            "searched_at": utc_now(),
            "queued": False,
            "requested_count": len(holdings),
            "updated_count": len(results),
            "error_count": len(errors),
            "results": results,
            "errors": errors[:50],
            "requested_fields": ["dividend", "price", "nav"],
            "source_catalog": market_source_catalog(),
            "data_policy": "source-attributed; unresolved or conflicting values are not invented",
            "recognition": {
                "recognized_count": sum(
                    1
                    for item in holdings
                    if (item.get("recognition") or {}).get("status") == "recognized"
                ),
                "unresolved_count": len(errors),
                "strategy": "normalized-identity-exact-code-name-search-and-source-verification",
            },
        }

    def _search_holding(self, holding: dict[str, Any]) -> dict[str, Any]:
        holding = recognize_holding_identity(holding)
        asset_type = str(holding.get("asset_type") or "").upper()
        market = str(holding.get("market") or "").upper()
        symbol = str(holding.get("symbol") or "").strip().upper()
        if asset_type == "FUND" or market == "FUND" or symbol.startswith("FUND-"):
            return self._with_verification(self._search_fund(holding), holding)
        yahoo = self._search_yahoo(holding)
        if yahoo.get("ok") is not True and holding.get("name"):
            resolved = self._resolve_yahoo_identity(holding)
            if resolved.get("ok") is True:
                holding = {
                    **holding,
                    "symbol": resolved["resolved_symbol"],
                    "market": resolved.get("market") or market,
                    "asset_type": resolved.get("asset_type") or asset_type,
                }
                market = str(holding.get("market") or "").upper()
                yahoo = self._search_yahoo(holding)
                if yahoo.get("ok") is True:
                    yahoo["identity_resolution"] = resolved
        if market != "TW":
            return self._with_verification(yahoo, holding)
        official = self._search_tw_official(holding)
        if official.get("ok") is not True:
            return self._with_verification(yahoo, holding)
        if yahoo.get("ok") is not True:
            return self._with_verification(official, holding)
        return self._with_verification(
            self._merge_tw_official_result(yahoo, official), holding
        )

    def _resolve_yahoo_identity(self, holding: dict[str, Any]) -> dict[str, Any]:
        name = str(holding.get("name") or "").strip()
        if not name:
            return {"ok": False, "message": "缺少可辨識名稱"}
        url = YAHOO_SEARCH_URL + "?" + urllib.parse.urlencode(
            {
                "q": name,
                "quotesCount": 12,
                "newsCount": 0,
                "enableFuzzyQuery": "true",
            }
        )
        payload = self.fetch_json(url, None)
        raw_quotes = payload.get("quotes") if isinstance(payload, dict) else []
        candidates: list[dict[str, Any]] = []
        expected_name = _normalized_name(name)
        expected_market = str(holding.get("market") or "").upper()
        expected_currency = str(holding.get("currency") or "").upper()
        for raw in raw_quotes if isinstance(raw_quotes, list) else []:
            if not isinstance(raw, dict):
                continue
            quote_type = str(raw.get("quoteType") or "").upper()
            symbol = _normalized_identifier(raw.get("symbol"))
            if not symbol or quote_type not in SUPPORTED_QUOTE_TYPES:
                continue
            candidate_name = str(raw.get("longname") or raw.get("shortname") or symbol)
            score = SequenceMatcher(
                None, expected_name, _normalized_name(candidate_name)
            ).ratio()
            candidate_market = self._market_from_yahoo_candidate(raw, symbol)
            if expected_market and candidate_market == expected_market:
                score += 0.14
            candidate_currency = str(raw.get("currency") or "").upper()
            if expected_currency and candidate_currency == expected_currency:
                score += 0.08
            candidates.append(
                {
                    "symbol": symbol,
                    "name": candidate_name,
                    "market": candidate_market,
                    "asset_type": "FUND" if quote_type == "MUTUALFUND" else quote_type,
                    "currency": candidate_currency,
                    "exchange": str(raw.get("exchange") or raw.get("exchDisp") or ""),
                    "confidence": round(min(0.99, score), 4),
                }
            )
        candidates.sort(key=lambda item: float(item["confidence"]), reverse=True)
        best = candidates[0] if candidates else None
        if best is None or float(best["confidence"]) < 0.64:
            return {
                "ok": False,
                "message": "名稱搜尋沒有足夠可信的標的",
                "candidate_count": len(candidates),
            }
        return {
            "ok": True,
            "resolved_symbol": best["symbol"],
            "resolved_name": best["name"],
            "market": best["market"],
            "asset_type": best["asset_type"],
            "currency": best["currency"],
            "confidence": best["confidence"],
            "source": "Yahoo Finance Search",
            "source_url": url,
            "candidates": candidates[:5],
        }

    @staticmethod
    def _market_from_yahoo_candidate(raw: dict[str, Any], symbol: str) -> str:
        for suffix, market in sorted(
            MARKET_SUFFIXES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if symbol.endswith(suffix):
                return market
        exchange = str(raw.get("exchange") or raw.get("exchDisp") or "").upper()
        if exchange in {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "NASDAQ", "NYSE"}:
            return "US"
        return ""

    @staticmethod
    def _with_verification(
        result: dict[str, Any], holding: dict[str, Any]
    ) -> dict[str, Any]:
        if result.get("ok") is not True:
            return result
        enriched = copy.deepcopy(result)
        sources = [
            item for item in enriched.get("sources", []) if isinstance(item, dict)
        ]
        official = sum(
            1
            for item in sources
            if str(item.get("kind") or "").startswith("official-")
        )
        source_count = len(
            {
                (str(item.get("name") or ""), str(item.get("url") or ""))
                for item in sources
            }
        )
        recognition = holding.get("recognition")
        enriched["recognition"] = (
            dict(recognition) if isinstance(recognition, dict) else {}
        )
        enriched["verification"] = {
            "source_count": source_count,
            "official_source_count": official,
            "cross_checked": source_count >= 2,
            "grade": "A" if official and source_count >= 2 else "B" if official else "C",
            "manual_update_allowed": bool(enriched.get("trusted")) and (
                official > 0 or _number(enriched.get("confidence")) >= 0.78
            ),
        }
        return enriched

    def _search_tw_official(self, holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        if not symbol:
            return {"ok": False, "message": "缺少臺灣商品代碼"}
        datasets = (
            (
                "TWSE",
                f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL",
                "Code",
                "ClosingPrice",
                "Change",
                "Date",
                "臺灣證券交易所",
            ),
            (
                "TPEx",
                f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes",
                "SecuritiesCompanyCode",
                "Close",
                "Change",
                "Date",
                "證券櫃檯買賣中心",
            ),
        )
        matched: dict[str, Any] | None = None
        source: dict[str, Any] | None = None
        change = 0.0
        observed_at = ""
        for exchange, url, code_key, price_key, change_key, date_key, source_name in datasets:
            try:
                row = next(
                    (
                        item
                        for item in self._cached_dataset(url, 300)
                        if str(item.get(code_key) or "").strip().upper() == symbol
                    ),
                    None,
                )
            except (OSError, urllib.error.URLError, ValueError):
                row = None
            if row is None:
                continue
            price = _number(row.get(price_key), 0)
            if price <= 0:
                continue
            matched = {"exchange": exchange, "price": price, "row": row}
            change = _number(str(row.get(change_key) or "").replace("X", ""), 0)
            observed_at = _roc_date_iso(row.get(date_key)) or utc_now()
            source = {
                "name": source_name,
                "url": url,
                "kind": "official-market-close",
                "observed_at": observed_at,
            }
            break
        if matched is None or source is None:
            return {"ok": False, "message": f"臺灣官方行情來源找不到：{symbol}"}

        events: list[dict[str, Any]] = []
        dividend_datasets = (
            (
                f"{TWSE_BASE}/exchangeReport/TWT48U_ALL",
                "Code",
                "CashDividend",
                "Date",
                "臺灣證券交易所",
            ),
            (
                f"{TPEX_BASE}/tpex_exright_daily",
                "SecuritiesCompanyCode",
                "CashDividend",
                "Date",
                "證券櫃檯買賣中心",
            ),
        )
        for url, code_key, amount_key, date_key, source_name in dividend_datasets:
            try:
                rows = self._cached_dataset(url, 3600)
            except (OSError, urllib.error.URLError, ValueError):
                continue
            for row in rows:
                if str(row.get(code_key) or "").strip().upper() != symbol:
                    continue
                amount = _number(row.get(amount_key), -1)
                event_at = _roc_date_iso(row.get(date_key))
                if amount < 0 or not event_at:
                    continue
                events.append(
                    {
                        "observed_at": event_at,
                        "record_date": event_at[:10],
                        "amount_per_unit": amount,
                        "currency": "TWD",
                        "source_name": source_name,
                        "source_url": url,
                    }
                )
        frequency = _infer_frequency(events)
        price = float(matched["price"])
        previous_close = price - change if price - change > 0 else None
        trailing_start = datetime.now(timezone.utc) - timedelta(days=366)
        trailing = sum(
            _number(item.get("amount_per_unit"))
            for item in events
            if datetime.fromisoformat(str(item["observed_at"])) >= trailing_start
        )
        return {
            "ok": True,
            "identity_key": f"{str(matched['exchange']).lower()}:{symbol}",
            "requested_symbol": symbol,
            "requested_name": str(holding.get("name") or ""),
            "resolved_symbol": symbol,
            "official_code": symbol,
            "isin": str(holding.get("isin") or ""),
            "name": str(holding.get("name") or symbol),
            "market": "TW",
            "asset_type": str(holding.get("asset_type") or "AUTO").upper(),
            "currency": "TWD",
            "quote_kind": "market_price",
            "observed_at": observed_at,
            "confidence": 0.96,
            "trusted": True,
            "parameters": {
                "price": price,
                "previous_close": previous_close,
                "change_percent": (change / previous_close * 100) if previous_close else None,
                "annual_distribution_per_unit": trailing,
                "distribution_yield_percent": (trailing / price * 100) if trailing > 0 else None,
                "distribution_frequency": frequency["code"],
            },
            "parameter_units": {
                "price": "currency",
                "previous_close": "currency",
                "change_percent": "%",
                "annual_distribution_per_unit": "currency",
                "distribution_yield_percent": "%",
            },
            "distribution": {
                "frequency": frequency["code"],
                "frequency_label": frequency["label"],
                "frequency_per_year": frequency["per_year"],
                "frequency_confidence": frequency["confidence"],
                "trailing_annual_per_unit": trailing,
                "event_count": len(events),
            },
            "distribution_events": events,
            "sources": [source],
        }

    @staticmethod
    def _merge_tw_official_result(
        yahoo: dict[str, Any], official: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(yahoo)
        official_parameters = official.get("parameters") or {}
        parameters = merged.setdefault("parameters", {})
        for key in ("price", "previous_close", "change_percent"):
            if official_parameters.get(key) is not None:
                parameters[key] = official_parameters[key]
        merged["observed_at"] = official.get("observed_at") or merged.get("observed_at")
        merged["currency"] = official.get("currency") or merged.get("currency")
        merged["confidence"] = max(_number(merged.get("confidence")), 0.96)
        merged["trusted"] = True
        merged["identity_key"] = official.get("identity_key") or merged.get("identity_key")
        merged["official_code"] = official.get("official_code") or merged.get("official_code")
        sources = [
            *[item for item in official.get("sources", []) if isinstance(item, dict)],
            *[item for item in merged.get("sources", []) if isinstance(item, dict)],
        ]
        merged["sources"] = list(
            {
                (str(item.get("name") or ""), str(item.get("url") or "")): item
                for item in sources
            }.values()
        )
        official_events = [
            item for item in official.get("distribution_events", []) if isinstance(item, dict)
        ]
        yahoo_events = [
            item for item in merged.get("distribution_events", []) if isinstance(item, dict)
        ]
        events = list(
            {
                (
                    str(item.get("record_date") or item.get("observed_at") or ""),
                    _number(item.get("amount_per_unit"), -1),
                ): item
                for item in [*official_events, *yahoo_events]
            }.values()
        )
        merged["distribution_events"] = events
        frequency = _infer_frequency(events)
        trailing_start = datetime.now(timezone.utc) - timedelta(days=366)
        trailing = sum(
            _number(item.get("amount_per_unit"))
            for item in events
            if str(item.get("observed_at") or "")
            and datetime.fromisoformat(str(item["observed_at"])) >= trailing_start
        )
        price = _number(parameters.get("price"), 0)
        parameters["annual_distribution_per_unit"] = trailing
        parameters["distribution_yield_percent"] = (
            trailing / price * 100 if trailing > 0 and price > 0 else None
        )
        parameters["distribution_frequency"] = frequency["code"]
        merged["distribution"] = {
            "frequency": frequency["code"],
            "frequency_label": frequency["label"],
            "frequency_per_year": frequency["per_year"],
            "frequency_confidence": frequency["confidence"],
            "trailing_annual_per_unit": trailing,
            "event_count": len(events),
        }
        return merged

    def _search_fund(self, holding: dict[str, Any]) -> dict[str, Any]:
        name = str(holding.get("name") or "").strip()
        symbol = str(holding.get("symbol") or "").strip().upper()
        currency = str(holding.get("currency") or "").upper()
        fund_code = _normalized_identifier(holding.get("fund_code"))
        isin = _normalized_identifier(holding.get("fund_isin") or holding.get("isin"))
        if not name and not fund_code and not isin:
            return {"ok": False, "message": "共同基金缺少名稱、基金代碼或 ISIN，無法辨識級別"}
        candidates: list[dict[str, Any]] = []
        used_terms: list[str] = []
        for term in _fund_query_terms(name, fund_code=fund_code, isin=isin):
            used_terms.append(term)
            try:
                response = self.fetch_json(
                    FUNDCLEAR_SEARCH_URL,
                    {
                        "_pageNum": 1,
                        "_pageSize": 25,
                        "column": "",
                        "asc": False,
                        "fundSite": "all",
                        "searchKey": term,
                        "fundTypeList": ["all"],
                        "currencyList": ["all"],
                        "asiFreqList": ["all"],
                        "fundRiskLevelList": ["all"],
                    },
                )
            except urllib.error.HTTPError as error:
                if error.code not in {400, 404}:
                    raise
                response = {}
            candidates = [item for item in response.get("data", []) if isinstance(item, dict)]
            if candidates:
                break
        if not candidates:
            return {"ok": False, "message": f"基金資訊觀測站找不到：{name}"}
        wanted = _normalized_name(name)
        wants_distribution = any(token in name for token in ("配息", "月配", "週配", "季配", "入息", "收益"))

        def score(item: dict[str, Any]) -> float:
            candidate_name = str(item.get("fundName") or "")
            ratio = SequenceMatcher(None, wanted, _normalized_name(candidate_name)).ratio()
            candidate_code = _normalized_identifier(item.get("fundCode"))
            candidate_isin = _normalized_identifier(
                item.get("isin") or item.get("fundIsin") or item.get("ISIN")
            )
            if fund_code and candidate_code == fund_code:
                ratio = max(ratio, 0.98)
            if isin and candidate_isin == isin:
                ratio = max(ratio, 0.99)
            candidate_currency = _currency_code(str(item.get("currencyName") or ""))
            if currency and candidate_currency == currency:
                ratio += 0.12
            declared = str(item.get("asiFreq") or "")
            if wants_distribution and ("配" in declared or "分配" in declared) and "不配息" not in declared:
                ratio += 0.1
            if not wants_distribution and ("不配息" in declared or "不分配" in declared):
                ratio += 0.04
            return ratio

        best = max(candidates, key=score)
        confidence = min(0.99, score(best))
        if confidence < 0.62:
            return {"ok": False, "message": f"基金名稱匹配可信度不足：{name}"}
        official_code = str(best.get("fundCode") or "")
        fund_site = str(best.get("fundSite") or "").lower()
        observed_date = str(best.get("navTxnDate") or "").replace("/", "-")
        observed_at = f"{observed_date}T00:00:00+08:00" if observed_date else utc_now()
        source_url = f"{FUNDCLEAR_BASE}/fund-basic-info?" + urllib.parse.urlencode(
            {"key": official_code, "site": fund_site, "tab": "fund-nav"}
        )
        declared_frequency = str(best.get("asiFreq") or "")
        distribution_events = self._fund_distribution_evidence(official_code, fund_site)
        frequency = _infer_frequency(distribution_events, declared_frequency)
        nav = _number(best.get("navValue"), 0)
        parameters: dict[str, Any] = {
            "price": nav,
            "ytd_return_percent": best.get("changeRatio"),
            "distribution_frequency": frequency["code"],
        }
        return {
            "ok": nav > 0,
            "identity_key": f"fundclear:{fund_site}:{official_code}",
            "requested_symbol": symbol,
            "requested_name": name,
            "resolved_symbol": official_code,
            "official_code": official_code,
            "isin": str(holding.get("isin") or ""),
            "name": str(best.get("fundName") or name),
            "market": "FUND",
            "asset_type": "FUND",
            "currency": _currency_code(str(best.get("currencyName") or currency)),
            "quote_kind": "nav",
            "observed_at": observed_at,
            "confidence": round(confidence, 4),
            "trusted": confidence >= 0.72 and nav > 0,
            "parameters": parameters,
            "parameter_units": {"price": "currency", "ytd_return_percent": "%"},
            "distribution": {
                "frequency": frequency["code"],
                "frequency_label": frequency["label"],
                "frequency_per_year": frequency["per_year"],
                "frequency_confidence": frequency["confidence"],
                "declared_text": declared_frequency,
                "event_count": len(distribution_events),
            },
            "distribution_events": distribution_events,
            "sources": [{"name": "基金資訊觀測站", "url": source_url, "kind": "official-fund-nav", "observed_at": observed_at}],
            "search_terms": used_terms,
            "identity_resolution": {
                "method": (
                    "exact-isin"
                    if isin
                    and _normalized_identifier(
                        best.get("isin") or best.get("fundIsin") or best.get("ISIN")
                    )
                    == isin
                    else "exact-fund-code"
                    if fund_code and _normalized_identifier(best.get("fundCode")) == fund_code
                    else "normalized-name-and-share-class"
                ),
                "confidence": round(confidence, 4),
                "candidate_count": len(candidates),
                "searched_terms": used_terms,
            },
        }

    def _fund_distribution_evidence(self, code: str, site: str) -> list[dict[str, Any]]:
        if not code:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=365 * 5)
        endpoint = f"{FUNDCLEAR_BASE}/api/{'offshore' if site == 'offshore' else 'onshore'}/fund-basic/query-latest-nav"
        response = self.fetch_json(
            endpoint,
            {
                "fundCode": code,
                "startDate": start.strftime("%Y/%m"),
                "endDate": end.strftime("%Y/%m"),
                "column": "",
                "asc": False,
                "_pageNum": 1,
                "_pageSize": 60,
            },
        )
        events: list[dict[str, Any]] = []
        for item in response.get("list", []):
            if not isinstance(item, dict):
                continue
            raw_date = str(item.get("infoDate") or "")
            observed = raw_date
            if re.fullmatch(r"\d{8}", raw_date):
                observed = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}T00:00:00+08:00"
            url = str(item.get("infoUrl") or "")
            title = str(item.get("infoContent") or "")
            if not raw_date.strip() and not title.strip():
                continue
            events.append({
                "observed_at": observed,
                "record_date": raw_date,
                "title": title,
                "amount_per_unit": None,
                "currency": "",
                "frequency": "",
                "source_name": "基金資訊觀測站／投信投顧公會",
                "source_url": url,
                "evidence_only": True,
            })
        return events

    def _search_yahoo(self, holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        candidates = _yahoo_symbol_candidates(symbol, market)
        if not candidates:
            return {"ok": False, "message": "缺少商品代碼"}
        requested = candidates[0]
        url = ""
        chart: dict[str, Any] = {}
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for candidate in candidates:
            candidate_url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(candidate, safe="") + "?" + urllib.parse.urlencode({"range": "5y", "interval": "1d", "events": "div"})
            try:
                payload = self.fetch_json(candidate_url, None)
            except (OSError, urllib.error.URLError) as error:
                errors.append(f"{candidate}: {type(error).__name__}")
                continue
            candidate_chart = payload.get("chart") or {}
            candidate_results = candidate_chart.get("result") or []
            if candidate_results:
                requested = candidate
                url = candidate_url
                chart = candidate_chart
                results = candidate_results
                break
            error = candidate_chart.get("error") or {}
            errors.append(
                f"{candidate}: {str(error.get('description') or 'no result')}"
            )
        if not results:
            return {
                "ok": False,
                "message": "公開市場來源無報價：" + "; ".join(errors[:4]),
                "searched_symbols": candidates,
            }
        result = results[0]
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        valid = [(int(ts), _number(close, 0)) for ts, close in zip(timestamps, closes) if _number(close, 0) > 0]
        price = _number(meta.get("regularMarketPrice"), 0) or (valid[-1][1] if valid else 0)
        observed_timestamp = int(meta.get("regularMarketTime") or (valid[-1][0] if valid else 0))
        observed_at = datetime.fromtimestamp(observed_timestamp, timezone.utc).isoformat() if observed_timestamp else utc_now()
        # With a multi-year chart Yahoo's chartPreviousClose can refer to the
        # beginning of the requested range.  The last two valid daily bars are
        # the only safe inputs for a one-day change calculation.
        previous_close = valid[-2][1] if len(valid) >= 2 else 0
        events: list[dict[str, Any]] = []
        trailing_start = datetime.now(timezone.utc) - timedelta(days=366)
        for item in ((result.get("events") or {}).get("dividends") or {}).values():
            timestamp = int(item.get("date") or 0)
            amount = _number(item.get("amount"), -1)
            if timestamp <= 0 or amount < 0:
                continue
            occurred = datetime.fromtimestamp(timestamp, timezone.utc)
            events.append({"observed_at": occurred.isoformat(), "record_date": occurred.date().isoformat(), "amount_per_unit": amount, "currency": str(meta.get("currency") or holding.get("currency") or "").upper(), "source_name": "Yahoo Finance", "source_url": url})
        frequency = _infer_frequency(events)
        trailing = sum(_number(item.get("amount_per_unit")) for item in events if datetime.fromisoformat(str(item["observed_at"])) >= trailing_start)
        parameters = {
            "price": price,
            "previous_close": previous_close or None,
            "change_percent": ((price - previous_close) / previous_close * 100) if previous_close > 0 else None,
            "annual_distribution_per_unit": trailing,
            "distribution_yield_percent": (trailing / price * 100) if trailing > 0 and price > 0 else None,
            "distribution_frequency": frequency["code"],
        }
        return {
            "ok": price > 0,
            "identity_key": f"yahoo:{requested}",
            "requested_symbol": symbol,
            "requested_name": str(holding.get("name") or ""),
            "resolved_symbol": requested,
            "official_code": "",
            "isin": str(holding.get("isin") or ""),
            "name": str(meta.get("longName") or meta.get("shortName") or holding.get("name") or symbol),
            "market": market,
            "asset_type": str(holding.get("asset_type") or "AUTO").upper(),
            "currency": str(meta.get("currency") or holding.get("currency") or "").upper(),
            "quote_kind": "market_price",
            "observed_at": observed_at,
            "confidence": 0.78,
            "trusted": price > 0,
            "parameters": parameters,
            "parameter_units": {"price": "currency", "previous_close": "currency", "change_percent": "%", "annual_distribution_per_unit": "currency", "distribution_yield_percent": "%"},
            "distribution": {"frequency": frequency["code"], "frequency_label": frequency["label"], "frequency_per_year": frequency["per_year"], "frequency_confidence": frequency["confidence"], "trailing_annual_per_unit": trailing, "event_count": len(events)},
            "distribution_events": events,
            "sources": [{"name": "Yahoo Finance", "url": url, "kind": "public-market-chart", "observed_at": observed_at}],
            "searched_symbols": candidates,
        }
