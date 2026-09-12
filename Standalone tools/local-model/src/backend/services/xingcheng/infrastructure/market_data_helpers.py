from __future__ import annotations

import json
import math
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
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


__all__ = [
    "FetchJson",
    "FUNDCLEAR_BASE",
    "FUNDCLEAR_SEARCH_URL",
    "TWSE_BASE",
    "TPEX_BASE",
    "YAHOO_SEARCH_URL",
    "NETWORK_DESTINATION_ALLOWLIST",
    "SUPPORTED_QUOTE_TYPES",
    "MARKET_SUFFIXES",
    "utc_now",
    "_number",
    "_fetch_json",
    "_normalized_name",
    "_normalized_identifier",
    "recognize_holding_identity",
    "_fund_query_terms",
    "_yahoo_symbol_candidates",
    "_currency_code",
    "_roc_date_iso",
    "_infer_frequency",
    "market_source_catalog",
]
