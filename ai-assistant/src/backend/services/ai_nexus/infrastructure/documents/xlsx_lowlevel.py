"""Split from portfolio_file.py."""
from __future__ import annotations

from .portfolio_models import *
from .portfolio_headers import *

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



def normalize_xlsx_column_mapping(column_mapping: Any) -> dict[str, int]:
    if not isinstance(column_mapping, dict):
        raise InvestmentManagerError("Excel column mapping must be an object.")
    normalized: dict[str, int] = {}
    for raw_field, raw_index in column_mapping.items():
        field = str(raw_field or "").strip()
        if field not in XLSX_MAPPING_FIELDS or raw_index is None or raw_index == "":
            continue
        if isinstance(raw_index, bool):
            raise InvestmentManagerError(f"Invalid column index for {field}.")
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise InvestmentManagerError(f"Invalid column index for {field}.") from exc
        if index < 0 or index >= XLSX_HEADER_SCAN_MAX_COLUMNS:
            raise InvestmentManagerError(
                f"Column index for {field} must be between 0 and {XLSX_HEADER_SCAN_MAX_COLUMNS - 1}."
            )
        normalized[field] = index
    missing = sorted(XLSX_REQUIRED_MAPPING_FIELDS - set(normalized))
    if missing:
        raise InvestmentManagerError(
            "Excel column mapping is missing required fields: " + ", ".join(missing)
        )
    return normalized


def xlsx_column_letter(index: int) -> str:
    if index < 0:
        return ""
    letters = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def load_xlsx_portfolio_with_mapping(
    path: Path,
    *,
    sheet_name: str,
    header_row_number: int,
    data_start_row_number: int | None = None,
    column_mapping: dict[str, Any],
) -> tuple[list[Holding], dict[str, Any]]:
    """Load holdings from an explicitly selected sheet, header row, and column map."""
    mapping = normalize_xlsx_column_mapping(column_mapping)
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc

    selected = next(
        (
            sheet
            for sheet in workbook_scan.get("sheets", [])
            if isinstance(sheet, dict) and str(sheet.get("sheet_name") or "") == sheet_name
        ),
        None,
    )
    if selected is None:
        raise InvestmentManagerError(f"Excel worksheet not found: {sheet_name}")
    rows = selected.get("rows")
    if not isinstance(rows, list) or not rows:
        raise InvestmentManagerError(f"Excel worksheet has no rows: {sheet_name}")
    try:
        header_number = int(header_row_number)
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError("Excel header row must be a positive integer.") from exc
    if header_number < 1 or header_number > len(rows):
        raise InvestmentManagerError(
            f"Excel header row must be between 1 and {len(rows)} for {sheet_name}."
        )
    try:
        data_start_number = int(
            header_number + 1
            if data_start_row_number is None
            else data_start_row_number
        )
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError("Excel data start row must be a positive integer.") from exc
    if data_start_number < 1 or data_start_number > len(rows):
        raise InvestmentManagerError(
            f"Excel data start row must be between 1 and {len(rows)} for {sheet_name}."
        )

    header_index = header_number - 1
    header_values = [str(value or "").strip() for value in rows[header_index]]
    holdings: list[Holding] = []
    non_empty_data_rows = 0
    for excel_row_number, values in enumerate(
        rows[data_start_number - 1 :],
        start=data_start_number,
    ):
        if not isinstance(values, list) or not any(str(value or "").strip() for value in values):
            continue
        non_empty_data_rows += 1
        mapped = {
            field: values[index] if index < len(values) else ""
            for field, index in mapping.items()
        }
        symbol = normalize_symbol(str(mapped.get("symbol") or ""))
        if not looks_like_portfolio_symbol(symbol):
            continue
        market = infer_market(
            symbol,
            str(mapped.get("market") or ""),
            str(mapped.get("asset_type") or ""),
        )
        holdings.append(
            Holding(
                symbol=symbol,
                name=str(mapped.get("name") or "").strip(),
                market=market,
                asset_type=str(mapped.get("asset_type") or "").strip(),
                quantity=parse_float(mapped.get("quantity")) or 0.0,
                average_cost=parse_float(mapped.get("average_cost")),
                currency=infer_currency(market, str(mapped.get("currency") or "")),
                principal_amount=parse_float(mapped.get("principal_amount")),
                principal_currency=str(
                    mapped.get("principal_currency")
                    or mapped.get("currency")
                    or infer_currency(market)
                ).strip().upper(),
                principal_twd=parse_float(mapped.get("principal_twd")),
                source_row=excel_row_number,
            )
        )
    if not holdings:
        raise InvestmentManagerError(
            "The selected Excel mapping produced no usable holdings. Check the header row, symbol, and quantity columns."
        )

    mapped_columns = {
        field: {
            "column_index": index,
            "column_letter": xlsx_column_letter(index),
            "header": header_values[index] if index < len(header_values) else "",
        }
        for field, index in mapping.items()
    }
    manual_sheet = {
        **public_xlsx_sheet_scan(selected),
        "usable": True,
        "score": 200 + len(mapping) * 10,
        "header_mode": "manual_mapping",
        "header_depth": 1,
        "header_row_index": header_index,
        "header_row_number": header_number,
        "header_start_row_index": header_index,
        "header_start_row_number": header_number,
        "data_start_row_index": data_start_number - 1,
        "data_start_row_number": data_start_number,
        "headers": header_values,
        "canonical_columns": list(mapping),
        "data_row_count": non_empty_data_rows,
        "valid_data_row_count": len(holdings),
        "manual_mapping": mapped_columns,
    }
    public_sheets = []
    for sheet in workbook_scan.get("sheets", []):
        public_sheet = public_xlsx_sheet_scan(sheet)
        if str(public_sheet.get("sheet_name") or "") == sheet_name:
            public_sheet = manual_sheet
        public_sheets.append(public_sheet)
    public_scan = {
        "sheet_count": len(public_sheets),
        "selected_sheet": manual_sheet,
        "sheets": public_sheets,
    }
    profile = {
        "sheet_name": sheet_name,
        "header_row_number": header_number,
        "data_start_row_number": data_start_number,
        "column_mapping": mapping,
        "mapped_columns": mapped_columns,
    }
    return holdings, {
        "profile": profile,
        "workbook_scan": public_scan,
        "imported_row_count": len(holdings),
        "skipped_row_count": max(0, non_empty_data_rows - len(holdings)),
    }


def xlsx_records_from_rows(
    rows: list[list[str]],
    header_index: int,
    headers: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for values in rows[header_index + 1:]:
        if not any(str(value or "").strip() for value in values):
            continue
        if xlsx_row_looks_like_header(values):
            continue
        row: dict[str, Any] = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            row[header] = values[index] if index < len(values) else ""
        records.append(row)
    return records


def xlsx_row_looks_like_header(values: Iterable[Any]) -> bool:
    cells = [str(value or "").strip() for value in values]
    if portfolio_header_score(cells) > 0:
        return True
    canonical_hit_count = sum(1 for value in cells if canonical_column(value))
    if canonical_hit_count >= 2:
        return True
    header_tokens = header_token_lookup()
    token_hits = sum(
        1
        for value in cells
        if normalize_header(value) in header_tokens
    )
    return token_hits >= 2


def looks_like_portfolio_symbol(value: Any) -> bool:
    text = normalize_symbol(str(value or ""))
    if not text:
        return False
    if normalize_header(text) in {
        "total",
        "subtotal",
        "updated",
        "updatedat",
        "date",
        "account",
        "cash",
        "us",
        "tw",
        "hk",
        "usd",
        "twd",
        "hkd",
        "etf",
        "\u5408\u8a08",
        "\u5c0f\u8a08",
        "\u7e3d\u8a08",
        "\u5e33\u6236",
        "\u66f4\u65b0\u6642\u9593",
    }:
        return False
    if re.fullmatch(r"\d{1,3}", text):
        return False
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:E[+-]?\d+)?", text):
        return False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    return bool(re.fullmatch(r"\^?[A-Z0-9][A-Z0-9./\-]{0,19}", text))


def looks_like_market_value(value: Any) -> bool:
    normalized = normalize_header(str(value or ""))
    return bool(normalized and normalized in market_alias_lookup())


def looks_like_currency_value(value: Any) -> bool:
    normalized = normalize_header(str(value or "")).upper()
    return normalized in {"USD", "TWD", "HKD", "JPY", "EUR", "CNY", "CNH", "GBP"}


def xlsx_row_looks_like_portfolio_data(values: Iterable[Any]) -> bool:
    cells = [str(value or "").strip() for value in values]
    if not any(cells) or xlsx_row_looks_like_header(cells):
        return False
    symbol_hits = sum(1 for value in cells if looks_like_portfolio_symbol(value))
    if symbol_hits <= 0:
        return False
    numeric_hits = sum(1 for value in cells if parse_float(value) is not None)
    market_hits = sum(1 for value in cells if looks_like_market_value(value))
    currency_hits = sum(1 for value in cells if looks_like_currency_value(value))
    return numeric_hits + market_hits + currency_hits > 0


def infer_xlsx_headers_from_data(
    rows: list[list[str]],
    header_index: int,
    source_headers: list[str],
    max_rows: int = 80,
) -> dict[str, Any] | None:
    width = max(
        [len(source_headers)]
        + [len(row) for row in rows[header_index + 1 : header_index + 1 + max_rows]]
    )
    stats = [
        {
            "symbol": 0,
            "numeric": 0,
            "market": 0,
            "currency": 0,
            "text": 0,
            "non_empty": 0,
        }
        for _ in range(width)
    ]

    for row in rows[header_index + 1 : header_index + 1 + max_rows]:
        values = [str(value or "").strip() for value in row]
        if not any(values):
            continue
        if xlsx_row_looks_like_header(values):
            continue
        for column_index in range(width):
            value = values[column_index] if column_index < len(values) else ""
            if not value:
                continue
            stats[column_index]["non_empty"] += 1
            if looks_like_market_value(value):
                stats[column_index]["market"] += 1
            elif looks_like_currency_value(value):
                stats[column_index]["currency"] += 1
            else:
                if parse_float(value) is not None:
                    stats[column_index]["numeric"] += 1
                if looks_like_portfolio_symbol(value):
                    stats[column_index]["symbol"] += 1
                if parse_float(value) is None and not looks_like_portfolio_symbol(value):
                    stats[column_index]["text"] += 1

    symbol_index = max(range(width), key=lambda index: stats[index]["symbol"], default=0)
    if not stats or stats[symbol_index]["symbol"] <= 0:
        return None

    canonical = canonical_header_map(source_headers + [""] * max(0, width - len(source_headers)))
    while len(canonical) < width:
        canonical.append("")
    canonical[symbol_index] = canonical[symbol_index] or "symbol"

    for column_index, column_stats in enumerate(stats):
        if canonical[column_index]:
            continue
        if column_stats["market"] > 0 and column_stats["market"] >= column_stats["symbol"]:
            canonical[column_index] = "market"
        elif column_stats["currency"] > 0:
            canonical[column_index] = "currency"

    numeric_columns = [
        index
        for index, column_stats in enumerate(stats)
        if index != symbol_index
        and not canonical[index]
        and column_stats["numeric"] > 0
    ]
    for column_index in numeric_columns:
        normalized_header = normalize_header(
            source_headers[column_index] if column_index < len(source_headers) else ""
        )
        if any(token in normalized_header for token in ("qty", "share", "unit", "amount", "\u6578", "\u80a1", "\u4efd", "\u5eab\u5b58")):
            canonical[column_index] = "quantity"
        elif any(token in normalized_header for token in ("cost", "price", "avg", "\u6210\u672c", "\u50f9", "\u5747", "\u5165\u5834")):
            canonical[column_index] = "average_cost"

    remaining_numeric = [index for index in numeric_columns if not canonical[index]]
    if "quantity" not in canonical and remaining_numeric:
        canonical[remaining_numeric.pop(0)] = "quantity"
    if "average_cost" not in canonical and remaining_numeric:
        canonical[remaining_numeric.pop(0)] = "average_cost"

    if "name" not in canonical:
        text_candidates = [
            index
            for index, column_stats in enumerate(stats)
            if index != symbol_index
            and not canonical[index]
            and column_stats["text"] > 0
        ]
        if text_candidates:
            canonical[min(text_candidates, key=lambda index: abs(index - symbol_index))] = "name"

    inferred_headers = [
        canonical[index] if canonical[index] else (source_headers[index] if index < len(source_headers) else "")
        for index in range(width)
    ]
    return {
        "headers": inferred_headers,
        "canonical_columns": canonical_columns(inferred_headers),
        "symbol_column_index": symbol_index,
        "inferred_column_count": sum(1 for item in canonical if item),
    }


def xlsx_data_profile(
    rows: list[list[str]],
    header_index: int,
    headers: list[str],
    max_rows: int = 80,
) -> dict[str, int]:
    mapped = canonical_header_map(headers)
    symbol_index = next(
        (index for index, canonical in enumerate(mapped) if canonical == "symbol"),
        None,
    )
    profile = {
        "non_empty_row_count": 0,
        "valid_data_row_count": 0,
        "symbol_row_count": 0,
        "quantity_row_count": 0,
        "average_cost_row_count": 0,
        "market_row_count": 0,
        "supporting_field_row_count": 0,
    }
    if symbol_index is None:
        return profile

    for row in rows[header_index + 1 : header_index + 1 + max_rows]:
        values = [str(value or "").strip() for value in row]
        if not any(values):
            continue
        if xlsx_row_looks_like_header(values):
            continue
        profile["non_empty_row_count"] += 1
        symbol = values[symbol_index] if symbol_index < len(values) else ""
        if not looks_like_portfolio_symbol(symbol):
            continue
        profile["symbol_row_count"] += 1

        has_supporting_field = False
        for column_index, canonical in enumerate(mapped):
            value = values[column_index] if column_index < len(values) else ""
            if not canonical or canonical == "symbol" or not value:
                continue
            has_supporting_field = True
            if canonical == "quantity" and parse_float(value) is not None:
                profile["quantity_row_count"] += 1
            elif canonical == "average_cost" and parse_float(value) is not None:
                profile["average_cost_row_count"] += 1
            elif canonical == "market" and normalize_market(value):
                profile["market_row_count"] += 1
        if has_supporting_field:
            profile["supporting_field_row_count"] += 1
        profile["valid_data_row_count"] += 1
    return profile


def xlsx_sheet_name_score(sheet_name: str) -> int:
    normalized = normalize_header(sheet_name)
    positive = (
        "portfolio",
        "holdings",
        "positions",
        "stock",
        "\u6301\u80a1",
        "\u5eab\u5b58",
        "\u6295\u8cc7",
        "\u90e8\u4f4d",
    )
    negative = (
        "summary",
        "overview",
        "history",
        "transaction",
        "trade",
        "\u6458\u8981",
        "\u7e3d\u89bd",
        "\u640d\u76ca",
        "\u4ea4\u6613",
        "\u6b77\u53f2",
        "\u660e\u7d30",
    )
    score = 0
    if any(token in normalized for token in positive):
        score += 30
    if any(token in normalized for token in negative):
        score -= 30
    return score


def score_xlsx_header_candidate(
    rows: list[list[str]],
    index: int,
    headers: list[str],
    sheet_name: str = "",
    header_start_index: int | None = None,
    header_depth: int = 1,
) -> dict[str, Any] | None:
    source_headers = headers
    header_mode = "headerless_inferred" if header_depth == 0 else "explicit"
    inferred = infer_xlsx_headers_from_data(rows, index, source_headers)
    column_score = portfolio_header_score(source_headers)
    if column_score <= 0:
        if inferred is None:
            return None
        headers = [str(header) for header in inferred["headers"]]
        column_score = 70 + int(inferred.get("inferred_column_count", 0)) * 12
        header_mode = "headerless_inferred" if header_depth == 0 else "inferred"
    elif inferred is not None:
        inferred_headers = [str(header) for header in inferred["headers"]]
        merged_headers: list[str] = []
        for column_index, header in enumerate(source_headers):
            if canonical_column(header):
                merged_headers.append(header)
            elif column_index < len(inferred_headers) and canonical_column(inferred_headers[column_index]):
                merged_headers.append(inferred_headers[column_index])
            else:
                merged_headers.append(header)
        headers = merged_headers
        header_mode = "explicit+inferred"
    data_profile = xlsx_data_profile(rows, index, headers)
    data_score = (
        data_profile["valid_data_row_count"] * 45
        + data_profile["quantity_row_count"] * 12
        + data_profile["average_cost_row_count"] * 10
        + data_profile["market_row_count"] * 8
        + data_profile["supporting_field_row_count"] * 8
    )
    if data_profile["valid_data_row_count"] == 0:
        data_score -= 80
    if xlsx_sheet_name_score(sheet_name) < 0 and data_profile["valid_data_row_count"] < 2:
        data_score -= 120
    score = column_score + data_score + xlsx_sheet_name_score(sheet_name)
    return {
        "header_row_index": index,
        "header_row_number": index + 1 if index >= 0 else None,
        "header_start_row_index": header_start_index if header_start_index is not None else index,
        "header_start_row_number": (
            (header_start_index if header_start_index is not None else index) + 1
            if (header_start_index if header_start_index is not None else index) >= 0
            else None
        ),
        "data_start_row_index": index + 1,
        "data_start_row_number": index + 2,
        "header_depth": header_depth,
        "headers": headers,
        "source_headers": source_headers,
        "header_mode": header_mode,
        "canonical_columns": canonical_columns(headers),
        "column_score": column_score,
        "data_score": data_score,
        "score": score,
        **data_profile,
    }


def combine_xlsx_header_rows(header_rows: list[list[str]]) -> list[str]:
    width = max((len(row) for row in header_rows), default=0)
    combined: list[str] = []
    for column_index in range(width):
        parts: list[str] = []
        for row in header_rows:
            value = str(row[column_index] if column_index < len(row) else "").strip()
            if value and value not in parts:
                parts.append(value)
        combined.append(" ".join(parts))
    return combined


def xlsx_header_candidates(
    rows: list[list[str]],
    sheet_name: str = "",
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    headerless_candidate_added = False
    scan_rows = [
        [str(value or "").strip() for value in row[:XLSX_HEADER_SCAN_MAX_COLUMNS]]
        for row in rows[:XLSX_HEADER_SCAN_MAX_ROWS]
    ]
    for index, headers in enumerate(scan_rows):
        candidate_headers: list[tuple[int, int, list[str]]] = []
        if not headerless_candidate_added and xlsx_row_looks_like_portfolio_data(headers):
            candidate_headers.append((index - 1, 0, [""] * len(headers)))
            headerless_candidate_added = True
        if any(headers):
            candidate_headers.append((index, 1, headers))
        if index > 0 and any(scan_rows[index - 1]):
            candidate_headers.append(
                (
                    index - 1,
                    2,
                    combine_xlsx_header_rows([scan_rows[index - 1], headers]),
                )
            )
        if (
            index > 1
            and any(scan_rows[index - 1])
            and any(scan_rows[index - 2])
        ):
            candidate_headers.append(
                (
                    index - 2,
                    3,
                    combine_xlsx_header_rows(
                        [
                            scan_rows[index - 2],
                            scan_rows[index - 1],
                            headers,
                        ]
                    ),
                )
            )
        for header_start_index, header_depth, candidate_header_values in candidate_headers:
            if header_depth != 0 and not any(candidate_header_values):
                continue
            column_score = portfolio_header_score(candidate_header_values)
            non_empty_header_cells = sum(
                1 for value in candidate_header_values if str(value or "").strip()
            )
            if (
                header_depth != 0
                and column_score <= 0
                and (
                    header_depth != 1
                    or header_start_index >= XLSX_INFERRED_HEADER_SCAN_ROWS
                    or non_empty_header_cells < 2
                )
            ):
                continue
            score_index = header_start_index if header_depth == 0 else index
            candidate = score_xlsx_header_candidate(
                rows,
                score_index,
                candidate_header_values,
                sheet_name,
                header_start_index=header_start_index,
                header_depth=header_depth,
            )
            if candidate is not None:
                candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (
            int(item.get("score", 0)),
            int(item.get("header_depth", 1)),
            -int(item.get("header_row_index", 0)),
        ),
        reverse=True,
    )


def find_portfolio_header_row(rows: list[list[str]]) -> tuple[int, list[str]] | None:
    candidates = xlsx_header_candidates(rows)
    if not candidates:
        return None
    best = candidates[0]
    return int(best["header_row_index"]), [str(header) for header in best["headers"]]


def scan_xlsx_workbook(path: Path, include_rows: bool = False) -> dict[str, Any]:
    namespace = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with _open_xlsx_workbook_unlocked(path) as workbook:
        shared_strings = read_xlsx_shared_strings(workbook, namespace)
        sheet_entries = xlsx_sheet_entries(workbook)
        sheets: list[dict[str, Any]] = []

        for sheet_index, (sheet_name, sheet_path) in enumerate(sheet_entries):
            sheet_root = ET.fromstring(workbook.read(sheet_path))
            rows = xlsx_rows_from_root(sheet_root, shared_strings, namespace)
            header_candidates = xlsx_header_candidates(rows, sheet_name)
            sheet_scan: dict[str, Any] = {
                "sheet_index": sheet_index,
                "sheet_name": sheet_name,
                "sheet_path": sheet_path,
                "row_count": len(rows),
                "usable": False,
                "score": 0,
                "header_row_index": None,
                "header_row_number": None,
                "headers": [],
                "canonical_columns": [],
                "data_row_count": 0,
                "valid_data_row_count": 0,
                "header_candidates": [
                    public_xlsx_header_candidate(candidate)
                    for candidate in header_candidates[:5]
                ],
            }
            if header_candidates:
                best_header = header_candidates[0]
                header_index = int(best_header["header_row_index"])
                headers = [str(header) for header in best_header.get("headers", [])]
                records = xlsx_records_from_rows(rows, header_index, headers)
                canonical = canonical_columns(headers)
                valid_data_rows = int(best_header.get("valid_data_row_count", 0))
                usable = valid_data_rows > 0 and not (
                    xlsx_sheet_name_score(sheet_name) < 0 and valid_data_rows < 2
                )
                public_valid_data_rows = valid_data_rows if usable else 0
                sheet_scan.update(
                    {
                        "usable": usable,
                        "score": int(best_header.get("score", 0)),
                        "column_score": int(best_header.get("column_score", 0)),
                        "data_score": int(best_header.get("data_score", 0)),
                        "header_mode": str(best_header.get("header_mode", "")),
                        "source_headers": best_header.get("source_headers", []),
                        "header_start_row_index": best_header.get("header_start_row_index"),
                        "header_start_row_number": best_header.get("header_start_row_number"),
                        "header_depth": best_header.get("header_depth", 1),
                        "header_row_index": header_index,
                        "header_row_number": best_header.get("header_row_number"),
                        "data_start_row_index": best_header.get("data_start_row_index"),
                        "data_start_row_number": best_header.get("data_start_row_number"),
                        "headers": headers,
                        "canonical_columns": canonical,
                        "data_row_count": len(records),
                        "valid_data_row_count": public_valid_data_rows,
                    }
                )
            if include_rows:
                sheet_scan["rows"] = rows
            sheets.append(sheet_scan)

    selected_sheet = next(
        (
            public_xlsx_sheet_scan(sheet)
            for sheet in sorted(
                sheets,
                key=lambda item: int(item.get("score", 0)),
                reverse=True,
            )
            if sheet.get("usable")
        ),
        None,
    )
    return {
        "sheet_count": len(sheets),
        "selected_sheet": selected_sheet,
        "sheets": sheets if include_rows else [public_xlsx_sheet_scan(sheet) for sheet in sheets],
    }


def public_xlsx_sheet_scan(sheet: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in sheet.items()
        if key != "rows"
    }


def public_xlsx_header_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    public_keys = {
        "header_row_index",
        "header_row_number",
        "header_start_row_index",
        "header_start_row_number",
        "data_start_row_index",
        "data_start_row_number",
        "header_depth",
        "header_mode",
        "headers",
        "source_headers",
        "canonical_columns",
        "column_score",
        "data_score",
        "score",
        "non_empty_row_count",
        "valid_data_row_count",
        "symbol_row_count",
        "quantity_row_count",
        "average_cost_row_count",
        "market_row_count",
        "supporting_field_row_count",
    }
    return {key: value for key, value in candidate.items() if key in public_keys}


def xlsx_rows(path: Path) -> list[list[str]]:
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with _open_xlsx_workbook_unlocked(path) as workbook:
        shared_strings = read_xlsx_shared_strings(workbook, namespace)
        _sheet_title, sheet_name = xlsx_sheet_entries(workbook)[0]
        sheet_root = ET.fromstring(workbook.read(sheet_name))

    return xlsx_rows_from_root(sheet_root, shared_strings, namespace)


def xlsx_rows_from_root(
    sheet_root: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> list[list[str]]:
    output: list[list[str]] = []
    for row in sheet_root.findall(".//m:sheetData/m:row", namespace):
        try:
            row_number = int(str(row.attrib.get("r", "")))
        except ValueError:
            row_number = len(output) + 1
        while len(output) < max(0, row_number - 1):
            output.append([])
        values: list[str] = []
        for cell in row.findall("m:c", namespace):
            index = xlsx_column_index(str(cell.attrib.get("r", "")))
            while len(values) < index:
                values.append("")
            values.append(read_xlsx_cell(cell, shared_strings, namespace))
        if row_number <= 0:
            output.append(values)
        elif len(output) == row_number - 1:
            output.append(values)
        else:
            output[row_number - 1] = values
    apply_xlsx_merged_cells(output, sheet_root, namespace)
    return output


def apply_xlsx_merged_cells(
    rows: list[list[str]],
    sheet_root: ET.Element,
    namespace: dict[str, str],
) -> None:
    for merged_cell in sheet_root.findall(".//m:mergeCells/m:mergeCell", namespace):
        reference = str(merged_cell.attrib.get("ref", ""))
        coordinates = xlsx_range_coordinates(reference)
        if coordinates is None:
            continue
        start_row, start_column, end_row, end_column = coordinates
        while len(rows) <= start_row:
            rows.append([])
        while len(rows[start_row]) <= start_column:
            rows[start_row].append("")
        value = rows[start_row][start_column]
        if not value:
            continue
        for row_index in range(start_row, end_row + 1):
            while len(rows) <= row_index:
                rows.append([])
            for column_index in range(start_column, end_column + 1):
                while len(rows[row_index]) <= column_index:
                    rows[row_index].append("")
                if not rows[row_index][column_index]:
                    rows[row_index][column_index] = value


def xlsx_sheet_entries(workbook: zipfile.ZipFile) -> list[tuple[str, str]]:
    names = set(workbook.namelist())
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        namespace = {
            "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
        }
        workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            str(rel.attrib.get("Id", "")): normalize_xlsx_relationship_target(
                "xl",
                str(rel.attrib.get("Target", "")),
            )
            for rel in rels_root.findall("rel:Relationship", namespace)
        }
        entries: list[tuple[str, str]] = []
        for index, sheet in enumerate(workbook_root.findall(".//m:sheets/m:sheet", namespace), start=1):
            sheet_name = str(sheet.attrib.get("name") or f"Sheet{index}")
            relation_id = str(
                sheet.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    "",
                )
            )
            sheet_path = relationships.get(relation_id, "")
            if sheet_path in names:
                entries.append((sheet_name, sheet_path))
        if entries:
            return entries

    fallback = sorted(
        (
            name
            for name in names
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ),
        key=natural_sort_key,
    )
    if not fallback:
        raise KeyError("xl/worksheets/sheet1.xml")
    return [(Path(name).stem, name) for name in fallback]


def normalize_xlsx_relationship_target(base_dir: str, target: str) -> str:
    raw = target.replace("\\", "/").strip()
    if raw.startswith("/"):
        path = PurePosixPath(raw.lstrip("/"))
    else:
        path = PurePosixPath(base_dir) / raw
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def natural_sort_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def first_xlsx_sheet_path(workbook: zipfile.ZipFile) -> str:
    return xlsx_sheet_entries(workbook)[0][1]


def read_xlsx_shared_strings(
    workbook: zipfile.ZipFile,
    namespace: dict[str, str],
) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("m:si", namespace):
        parts = [
            text.text or ""
            for text in item.findall(".//m:t", namespace)
        ]
        strings.append("".join(parts))
    return strings


def xlsx_column_index(reference: str) -> int:
    letters = re.sub(r"[^A-Za-z]", "", reference).upper()
    if not letters:
        return 0
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def xlsx_cell_coordinates(reference: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Za-z]+)\$?(\d+)", str(reference).strip())
    if not match:
        return None
    row_number = int(match.group(2))
    if row_number <= 0:
        return None
    return row_number - 1, xlsx_column_index(match.group(1))


def xlsx_range_coordinates(reference: str) -> tuple[int, int, int, int] | None:
    parts = [part.strip() for part in str(reference).split(":") if part.strip()]
    if not parts:
        return None
    start = xlsx_cell_coordinates(parts[0])
    end = xlsx_cell_coordinates(parts[-1])
    if start is None or end is None:
        return None
    start_row, start_column = start
    end_row, end_column = end
    return (
        min(start_row, end_row),
        min(start_column, end_column),
        max(start_row, end_row),
        max(start_column, end_column),
    )


def read_xlsx_cell(
    cell: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            text.text or ""
            for text in cell.findall(".//m:t", namespace)
        ).strip()

    value = cell.find("m:v", namespace)
    text = "" if value is None or value.text is None else value.text
    if cell_type == "s":
        try:
            return shared_strings[int(text)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if text == "1" else "FALSE"
    return text.strip()


def rows_to_holdings(rows: Iterable[Any]) -> list[Holding]:
    holdings: list[Holding] = []
    for row_index, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, dict):
            continue
        mapped: dict[str, Any] = {}
        for key, value in raw_row.items():
            canonical = canonical_column(str(key))
            if canonical:
                mapped[canonical] = value
        symbol = normalize_symbol(str(mapped.get("symbol") or raw_row.get("symbol") or ""))
        if not symbol:
            continue
        market = infer_market(
            symbol,
            str(mapped.get("market") or ""),
            str(mapped.get("asset_type") or ""),
        )
        quantity = parse_float(mapped.get("quantity"))
        average_cost = parse_float(mapped.get("average_cost"))
        currency = infer_currency(market, str(mapped.get("currency") or ""))
        holdings.append(
            Holding(
                symbol=symbol,
                name=str(mapped.get("name") or "").strip(),
                market=market,
                asset_type=str(mapped.get("asset_type") or "").strip(),
                quantity=quantity or 0.0,
                average_cost=average_cost,
                currency=currency,
                principal_amount=parse_float(mapped.get("principal_amount")),
                principal_currency=str(
                    mapped.get("principal_currency") or currency
                ).strip().upper(),
                principal_twd=parse_float(mapped.get("principal_twd")),
                source_row=row_index,
            )
        )
    if not holdings:
        raise InvestmentManagerError("Portfolio file has no usable holdings.")
    return holdings



__all__ = ['PROGRESS_JSON_PREFIX', 'DEFAULT_INTERVAL_SECONDS', 'DEFAULT_REQUEST_TIMEOUT_SECONDS', 'DEFAULT_PROVIDER_ORDER', 'DEFAULT_IMPORT_SNAPSHOT_KEEP', 'SNAPSHOT_COPY_ATTEMPTS', 'SNAPSHOT_COPY_CHUNK_BYTES', 'XLSX_INFERRED_HEADER_SCAN_ROWS', 'XLSX_HEADER_SCAN_MAX_ROWS', 'XLSX_HEADER_SCAN_MAX_COLUMNS', 'XLSX_HORIZONTAL_GROUP_WIDTH', 'CSV_EXTENSIONS', 'JSON_EXTENSIONS', 'XLSX_EXTENSIONS', 'LEGACY_EXCEL_EXTENSIONS', 'EXCEL_EXTENSIONS', 'COMMON_US_ETF_SYMBOLS', 'CRYPTO_ID_MAP', 'HEADER_ALIASES', 'XLSX_MAPPING_FIELDS', 'XLSX_REQUIRED_MAPPING_FIELDS', 'MARKET_ALIASES', 'MARKET_SESSIONS', 'normalize_xlsx_column_mapping', 'xlsx_column_letter', 'load_xlsx_portfolio_with_mapping', 'xlsx_records_from_rows', 'xlsx_row_looks_like_header', 'looks_like_portfolio_symbol', 'looks_like_market_value', 'looks_like_currency_value', 'xlsx_row_looks_like_portfolio_data', 'infer_xlsx_headers_from_data', 'xlsx_data_profile', 'xlsx_sheet_name_score', 'score_xlsx_header_candidate', 'combine_xlsx_header_rows', 'xlsx_header_candidates', 'find_portfolio_header_row', 'scan_xlsx_workbook', 'public_xlsx_sheet_scan', 'public_xlsx_header_candidate', 'xlsx_rows', 'xlsx_rows_from_root', 'apply_xlsx_merged_cells', 'xlsx_sheet_entries', 'normalize_xlsx_relationship_target', 'natural_sort_key', 'first_xlsx_sheet_path', 'read_xlsx_shared_strings', 'xlsx_column_index', 'xlsx_cell_coordinates', 'xlsx_range_coordinates', 'read_xlsx_cell', 'rows_to_holdings']
