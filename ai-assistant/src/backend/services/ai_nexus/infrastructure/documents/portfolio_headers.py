"""Split from portfolio_file.py."""
from __future__ import annotations

from .portfolio_models import *

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


def parse_float(value: Any) -> float | None:
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



__all__ = ['PROGRESS_JSON_PREFIX', 'DEFAULT_INTERVAL_SECONDS', 'DEFAULT_REQUEST_TIMEOUT_SECONDS', 'DEFAULT_PROVIDER_ORDER', 'DEFAULT_IMPORT_SNAPSHOT_KEEP', 'SNAPSHOT_COPY_ATTEMPTS', 'SNAPSHOT_COPY_CHUNK_BYTES', 'XLSX_INFERRED_HEADER_SCAN_ROWS', 'XLSX_HEADER_SCAN_MAX_ROWS', 'XLSX_HEADER_SCAN_MAX_COLUMNS', 'XLSX_HORIZONTAL_GROUP_WIDTH', 'CSV_EXTENSIONS', 'JSON_EXTENSIONS', 'XLSX_EXTENSIONS', 'LEGACY_EXCEL_EXTENSIONS', 'EXCEL_EXTENSIONS', 'COMMON_US_ETF_SYMBOLS', 'CRYPTO_ID_MAP', 'HEADER_ALIASES', 'XLSX_MAPPING_FIELDS', 'XLSX_REQUIRED_MAPPING_FIELDS', 'MARKET_ALIASES', 'MARKET_SESSIONS', 'normalize_header', 'normalized_header_tokens', 'header_alias_lookup', 'header_substring_aliases', 'market_alias_lookup', 'header_token_lookup', 'canonical_column', 'canonical_columns', 'canonical_header_map', 'portfolio_header_score', 'normalize_market', 'parse_float', 'infer_market', 'infer_currency', 'normalize_symbol']
