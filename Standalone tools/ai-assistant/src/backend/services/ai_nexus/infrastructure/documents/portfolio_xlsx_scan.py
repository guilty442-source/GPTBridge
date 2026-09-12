"""XLSX header scanning, candidate scoring, and workbook scanning."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .portfolio_constants import (
    XLSX_HEADER_SCAN_MAX_COLUMNS,
    XLSX_HEADER_SCAN_MAX_ROWS,
    XLSX_INFERRED_HEADER_SCAN_ROWS,
)
from .portfolio_utils import (
    canonical_column,
    canonical_columns,
    normalize_header,
    portfolio_header_score,
)
from .portfolio_xlsx_inference import (
    infer_xlsx_headers_from_data,
    looks_like_portfolio_symbol,
    xlsx_data_profile,
    xlsx_row_looks_like_header,
    xlsx_row_looks_like_portfolio_data,
)
from .portfolio_xlsx_lowlevel import (
    read_xlsx_shared_strings,
    xlsx_rows_from_root,
    xlsx_sheet_entries,
)
from .portfolio_snapshot import _open_xlsx_workbook_unlocked


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
