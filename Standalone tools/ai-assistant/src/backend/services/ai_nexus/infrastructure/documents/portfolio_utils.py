"""Utility functions: time, header normalization, market/symbol/currency helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable
from zoneinfo import ZoneInfo

from .portfolio_constants import HEADER_ALIASES, MARKET_ALIASES


def local_device_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> datetime:
    return local_device_now()


def utc_now_text() -> str:
    return utc_now().isoformat()


def timezone_for(name: str, now: datetime | None = None) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    if name in {"Asia/Taipei", "Asia/Hong_Kong"}:
        return timezone(timedelta(hours=8), name)
    if name == "America/New_York":
        reference = now or utc_now()
        offset = -4 if 3 <= reference.month <= 11 else -5
        return timezone(timedelta(hours=offset), name)
    return timezone.utc


@lru_cache(maxsize=1024)
def normalize_header(value: str) -> str:
    return re.sub(
        r"[\s_\-()\uff08\uff09./:：,，;；\[\]【】{}（）「」『』]+",
        "",
        str(value).strip().casefold(),
    )


def normalized_header_tokens(value: str) -> list[str]:
    return [
        token
        for token in (
            normalize_header(part)
            for part in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(value))
        )
        if token
    ]


@lru_cache(maxsize=1)
def header_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            normalized_alias = normalize_header(alias)
            if normalized_alias:
                lookup[normalized_alias] = canonical
    return lookup


@lru_cache(maxsize=1)
def header_substring_aliases() -> tuple[tuple[str, str], ...]:
    aliases: list[tuple[str, str]] = []
    for canonical, raw_aliases in HEADER_ALIASES.items():
        for alias in raw_aliases:
            normalized_alias = normalize_header(alias)
            if len(normalized_alias) >= 2:
                aliases.append((normalized_alias, canonical))
    return tuple(aliases)


@lru_cache(maxsize=1)
def market_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for market, aliases in MARKET_ALIASES.items():
        lookup[normalize_header(market)] = market
        for alias in aliases:
            normalized_alias = normalize_header(alias)
            if normalized_alias:
                lookup[normalized_alias] = market
    return lookup


@lru_cache(maxsize=1)
def header_token_lookup() -> frozenset[str]:
    return frozenset(
        normalize_header(token)
        for token in (
            "id",
            "code",
            "symbol",
            "ticker",
            "stock",
            "name",
            "market",
            "qty",
            "quantity",
            "shares",
            "units",
            "position",
            "price",
            "cost",
            "avg",
            "entry",
            "\u4ee3\u865f",
            "\u4ee3\u78bc",
            "\u540d\u7a31",
            "\u6578\u91cf",
            "\u6301\u5009",
            "\u6210\u672c",
            "\u50f9\u683c",
        )
    )


@lru_cache(maxsize=1024)
def canonical_column(header: str) -> str | None:
    normalized = normalize_header(header)
    if not normalized:
        return None
    alias_lookup = header_alias_lookup()
    if normalized in alias_lookup:
        return alias_lookup[normalized]

    token_matches = [
        (index, token, alias_lookup[token])
        for index, token in enumerate(normalized_header_tokens(header))
        if token in alias_lookup
    ]
    if token_matches:
        _index, token, canonical = token_matches[-1]
        if canonical == "symbol" and token in {"code", "id"}:
            previous = next(
                (
                    previous_canonical
                    for _previous_index, _previous_token, previous_canonical in reversed(token_matches[:-1])
                    if previous_canonical != "symbol"
                ),
                None,
            )
            if previous:
                return previous
        return canonical

    substring_matches: list[tuple[bool, int, bool, str]] = []
    for normalized_alias, canonical in header_substring_aliases():
        if normalized_alias not in normalized:
            continue
        suffix_match = normalized.endswith(normalized_alias)
        generic_symbol_suffix = canonical == "symbol" and normalized_alias in {"code", "id"}
        substring_matches.append(
            (
                suffix_match and not generic_symbol_suffix,
                len(normalized_alias),
                suffix_match,
                canonical,
            )
        )
    if substring_matches:
        return sorted(substring_matches, reverse=True)[0][3]
    return None


def canonical_columns(headers: Iterable[str]) -> list[str]:
    return [
        canonical
        for canonical in (canonical_column(header) for header in headers)
        if canonical
    ]


def canonical_header_map(headers: Iterable[str]) -> list[str]:
    mapped: list[str] = []
    seen: set[str] = set()
    for header in headers:
        canonical = canonical_column(header)
        if canonical and canonical not in seen:
            mapped.append(canonical)
            seen.add(canonical)
        else:
            mapped.append("")
    return mapped


def portfolio_header_score(headers: Iterable[str]) -> int:
    canonical_headers = set(canonical_columns(headers))
    if "symbol" not in canonical_headers:
        return 0
    score = 100 + len(canonical_headers) * 10
    for preferred in ("quantity", "market", "average_cost", "currency"):
        if preferred in canonical_headers:
            score += 20
    return score


def normalize_market(value: str) -> str:
    normalized = normalize_header(value)
    if not normalized:
        return ""
    return market_alias_lookup().get(normalized, normalized.upper())


def parse_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[$,%\s,\uff0c]", "", text)
    text = text.replace("NT", "").replace("TWD", "").replace("USD", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def infer_market(symbol: str, explicit_market: str = "", asset_type: str = "") -> str:
    market = normalize_market(explicit_market)
    if market:
        return market
    normalized_type = normalize_market(asset_type)
    if normalized_type in {"CRYPTO", "FUND", "INDEX"}:
        return normalized_type
    symbol_upper = symbol.strip().upper()
    if symbol_upper.endswith((".TW", ".TWO")):
        return "TW"
    if symbol_upper.endswith(".HK"):
        return "HK"
    if symbol_upper.endswith("-USD") or "/" in symbol_upper:
        return "CRYPTO"
    if symbol_upper.startswith("^"):
        return "INDEX"
    if re.fullmatch(r"\d{4,6}", symbol_upper):
        return "HK" if symbol_upper.startswith("0") else "TW"
    return "US"


def infer_currency(market: str, explicit_currency: str = "") -> str:
    currency = str(explicit_currency or "").strip().upper()
    if currency:
        return currency
    return {
        "US": "USD",
        "TW": "TWD",
        "HK": "HKD",
        "CRYPTO": "USD",
    }.get(market, "")


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()
