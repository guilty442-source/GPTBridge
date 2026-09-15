"""XLSX column-mapping helpers split from the portfolio documents module."""
from __future__ import annotations

from .portfolio_constants import *
from .portfolio_models import *
from .portfolio_utils import *
from .portfolio_xlsx_inference import *
from .portfolio_xlsx_lowlevel import *
from .portfolio_xlsx_scan import *

def xlsx_mapping_preview(
    path: Path,
    *,
    max_rows: int = XLSX_HEADER_SCAN_MAX_ROWS,
    max_columns: int = XLSX_HEADER_SCAN_MAX_COLUMNS,
) -> dict[str, Any]:
    """Return a bounded workbook preview suitable for an interactive column mapper."""
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc

    preview_sheets: list[dict[str, Any]] = []
    for raw_sheet in workbook_scan.get("sheets", []):
        rows = raw_sheet.get("rows") if isinstance(raw_sheet, dict) else None
        if not isinstance(rows, list):
            continue
        bounded_rows = [
            [str(value or "") for value in row[:max_columns]]
            for row in rows[:max_rows]
            if isinstance(row, list)
        ]
        width = min(
            max((len(row) for row in bounded_rows), default=0),
            max_columns,
        )
        headers = [str(value or "") for value in raw_sheet.get("headers", [])]
        suggested_mapping: dict[str, int] = {}
        for index, canonical in enumerate(canonical_header_map(headers)):
            if canonical and canonical not in suggested_mapping:
                suggested_mapping[canonical] = index
        preview_sheets.append(
            {
                "sheet_index": raw_sheet.get("sheet_index", len(preview_sheets)),
                "sheet_name": str(raw_sheet.get("sheet_name") or f"Sheet{len(preview_sheets) + 1}"),
                "row_count": int(raw_sheet.get("row_count") or 0),
                "preview_row_count": len(bounded_rows),
                "column_count": width,
                "suggested_header_row_number": raw_sheet.get("header_row_number"),
                "suggested_data_start_row_number": raw_sheet.get("data_start_row_number"),
                "suggested_mapping": suggested_mapping,
                "header_candidates": raw_sheet.get("header_candidates", []),
                "rows": bounded_rows,
            }
        )

    horizontal_layout = xlsx_horizontal_matrix_preview_from_scan(workbook_scan)
    consolidated_layout = xlsx_consolidated_report_preview_from_scan(workbook_scan)
    selected_sheet = workbook_scan.get("selected_sheet")
    selected_sheet_name = (
        str(selected_sheet.get("sheet_name") or "")
        if isinstance(selected_sheet, dict)
        else ""
    )
    if consolidated_layout.get("recommended"):
        selected_sheet_name = str(
            consolidated_layout.get("sheet_name") or selected_sheet_name
        )
    elif horizontal_layout.get("recommended"):
        preferred_sheet = next(
            (
                sheet
                for sheet in horizontal_layout.get("sheets", [])
                if isinstance(sheet, dict) and sheet.get("selected_by_default")
            ),
            None,
        )
        if isinstance(preferred_sheet, dict):
            selected_sheet_name = str(preferred_sheet.get("sheet_name") or selected_sheet_name)
    if not selected_sheet_name and preview_sheets:
        selected_sheet_name = str(preview_sheets[0]["sheet_name"])
    return {
        "sheet_count": len(preview_sheets),
        "selected_sheet_name": selected_sheet_name,
        "sheets": preview_sheets,
        "mapping_fields": list(XLSX_MAPPING_FIELDS),
        "required_fields": sorted(XLSX_REQUIRED_MAPPING_FIELDS),
        "preview_limits": {"rows": max_rows, "columns": max_columns},
        "horizontal_layout": horizontal_layout,
        "consolidated_layout": consolidated_layout,
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
