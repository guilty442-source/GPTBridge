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


FetchJson = Callable[[str, dict[str, Any] | None], dict[str, Any]]
FUNDCLEAR_BASE = "https://www.fundclear.com.tw"
FUNDCLEAR_SEARCH_URL = f"{FUNDCLEAR_BASE}/api/search/fund/query-fund"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _fetch_json(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
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


def _fund_query_terms(name: str) -> list[str]:
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
    candidates = [cleaned, f"{manager}{core[:8]}", f"{manager}{core[:5]}", manager]
    result: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip(" -–—_")
        if len(candidate) >= 2 and candidate not in result:
            result.append(candidate)
    return result[:4]


def _currency_code(value: str) -> str:
    text = str(value or "").upper()
    mapping = {
        "新台幣": "TWD", "台幣": "TWD", "美元": "USD", "澳幣": "AUD",
        "歐元": "EUR", "人民幣": "CNY", "日圓": "JPY", "港幣": "HKD",
        "南非幣": "ZAR", "英鎊": "GBP", "瑞士法郎": "CHF",
    }
    return mapping.get(text, text)


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
    """星澄的唯讀市場搜尋層；不讀取投資管家資料庫。"""

    def __init__(self, fetch_json: FetchJson | None = None) -> None:
        self.fetch_json = fetch_json or _fetch_json
        self._holding_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._holding_cache_lock = threading.Lock()

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
        holdings = [dict(item) for item in requested if isinstance(item, dict)][:300]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        workers = max(1, min(int(payload.get("max_workers") or 8), 12))
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
                        errors.append({"symbol": str(holding.get("symbol") or ""), "message": str(result.get("message") or "找不到可驗證資料")})
                except Exception as error:
                    errors.append({"symbol": str(holding.get("symbol") or ""), "message": f"{type(error).__name__}: {error}"})
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
            "data_policy": "source-attributed; unresolved or conflicting values are not invented",
        }

    def _search_holding(self, holding: dict[str, Any]) -> dict[str, Any]:
        asset_type = str(holding.get("asset_type") or "").upper()
        market = str(holding.get("market") or "").upper()
        symbol = str(holding.get("symbol") or "").strip().upper()
        if asset_type == "FUND" or market == "FUND" or symbol.startswith("FUND-"):
            return self._search_fund(holding)
        return self._search_yahoo(holding)

    def _search_fund(self, holding: dict[str, Any]) -> dict[str, Any]:
        name = str(holding.get("name") or "").strip()
        symbol = str(holding.get("symbol") or "").strip().upper()
        currency = str(holding.get("currency") or "").upper()
        if not name:
            return {"ok": False, "message": "共同基金缺少名稱，無法辨識級別"}
        candidates: list[dict[str, Any]] = []
        used_terms: list[str] = []
        for term in _fund_query_terms(name):
            used_terms.append(term)
            try:
                response = self.fetch_json(
                    FUNDCLEAR_SEARCH_URL,
                    {
                        "_pageNum": 1,
                        "_pageSize": 10,
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
        }

    def _fund_distribution_evidence(self, code: str, site: str) -> list[dict[str, Any]]:
        if not code:
            return []
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=730)
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
                "_pageSize": 24,
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
        requested = symbol
        if market == "TW" and symbol and not symbol.endswith((".TW", ".TWO")):
            requested = f"{symbol}.TW"
        if not requested:
            return {"ok": False, "message": "缺少商品代碼"}
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(requested, safe="") + "?" + urllib.parse.urlencode({"range": "2y", "interval": "1d", "events": "div"})
        payload = self.fetch_json(url, None)
        chart = payload.get("chart") or {}
        results = chart.get("result") or []
        if not results:
            error = chart.get("error") or {}
            return {"ok": False, "message": str(error.get("description") or "公開市場來源無報價")}
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
        }
