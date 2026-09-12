"""XLSX data inference: detect portfolio columns from row content."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .portfolio_utils import (
    canonical_column,
    canonical_columns,
    canonical_header_map,
    header_token_lookup,
    market_alias_lookup,
    normalize_header,
    normalize_market,
    normalize_symbol,
    parse_float,
    portfolio_header_score,
)


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
