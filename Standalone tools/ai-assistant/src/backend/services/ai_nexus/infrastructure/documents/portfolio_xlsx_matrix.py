"""Matrix helper functions shared by horizontal and consolidated XLSX layouts."""

from __future__ import annotations

import re
from typing import Any

from .portfolio_constants import COMMON_US_ETF_SYMBOLS
from .portfolio_utils import normalize_header, parse_float


def xlsx_matrix_label(value: Any) -> str:
    return normalize_header(str(value or ""))


def xlsx_matrix_group_starts(row: list[Any], labels: set[str]) -> list[int]:
    return [
        index
        for index, value in enumerate(row)
        if xlsx_matrix_label(value) in labels
    ]


def xlsx_matrix_numeric_value(
    row: list[Any],
    start_column_index: int,
    group_width: int,
) -> float | None:
    end = min(len(row), start_column_index + max(2, group_width))
    for value in row[start_column_index + 1 : end]:
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    return None


def xlsx_matrix_header_row(
    rows: list[list[Any]],
    holding_row_index: int,
    group_starts: list[int],
) -> int | None:
    search_start = max(0, holding_row_index - 32)
    for index in range(holding_row_index - 1, search_start - 1, -1):
        row = rows[index]
        first_label = xlsx_matrix_label(row[0] if row else "")
        if first_label in {"月份", "年月", "月分"}:
            return index
    minimum_labels = max(1, min(3, len(group_starts)))
    for index in range(holding_row_index - 1, search_start - 1, -1):
        row = rows[index]
        labels = [
            str(row[column] or "").strip()
            for column in group_starts
            if column < len(row) and str(row[column] or "").strip()
        ]
        if len(labels) >= minimum_labels:
            return index
    return None


def xlsx_matrix_related_row(
    rows: list[list[Any]],
    holding_row_index: int,
    group_starts: list[int],
    labels: set[str],
) -> int | None:
    for index in range(holding_row_index + 1, min(len(rows), holding_row_index + 7)):
        row = rows[index]
        if any(
            column < len(row) and xlsx_matrix_label(row[column]) in labels
            for column in group_starts
        ):
            return index
    return None


def xlsx_fund_currency(name: str, default_currency: str) -> str:
    normalized = normalize_header(name).upper()
    for tokens, currency in (
        (("美元", "美金", "USD"), "USD"),
        (("台幣", "新台幣", "TWD"), "TWD"),
        (("歐元", "EUR"), "EUR"),
        (("日圓", "日幣", "JPY"), "JPY"),
        (("澳幣", "AUD"), "AUD"),
        (("人民幣", "CNY"), "CNY"),
    ):
        if any(token.upper() in normalized for token in tokens):
            return currency
    return default_currency


def xlsx_horizontal_asset_type(symbol: str, market: str, configured: str) -> str:
    if configured and configured != "AUTO":
        return configured
    if market == "FUND":
        return "FUND"
    if market == "TW" and re.fullmatch(r"00\d{2,4}", symbol):
        return "ETF"
    if market == "US" and symbol in COMMON_US_ETF_SYMBOLS:
        return "ETF"
    return "STOCK"


def xlsx_horizontal_sheet_is_summary(sheet_name: str) -> bool:
    normalized = normalize_header(sheet_name)
    return any(
        token in normalized
        for token in ("總表", "報酬", "績效", "分析", "試算", "說明", "歷史")
    )


def xlsx_horizontal_sheet_defaults(sheet_name: str) -> dict[str, str]:
    normalized = normalize_header(sheet_name)
    if "基金" in normalized or any(token in normalized for token in ("鉅亨", "中租")):
        return {
            "category_label": "共同基金",
            "market": "FUND",
            "asset_type": "FUND",
            "currency": "TWD",
        }
    if "美股" in normalized or "美元" in normalized:
        return {
            "category_label": "美股 / ETF",
            "market": "US",
            "asset_type": "AUTO",
            "currency": "USD",
        }
    return {
        "category_label": "台股 / ETF",
        "market": "TW",
        "asset_type": "AUTO",
        "currency": "TWD",
    }
