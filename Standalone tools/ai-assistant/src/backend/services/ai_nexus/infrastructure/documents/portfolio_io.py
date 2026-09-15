"""Portfolio loading entry points split from the documents module."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .portfolio_models import *
from .portfolio_xlsx_consolidated import *
from .portfolio_xlsx_horizontal import *
from .portfolio_xlsx_mapping import *
from .portfolio_xlsx_scan import *
from .portfolio_snapshot import (
    _open_portfolio_source_shared,
    _read_portfolio_source_bytes,
    _read_portfolio_source_text,
    _source_revision_unchanged,
)

def load_portfolio(path: Path) -> list[Holding]:
    if not path.exists():
        raise InvestmentManagerError(f"Portfolio file not found: {path}")
    if not path.is_file():
        raise InvestmentManagerError(f"Portfolio path is not a file: {path}")
    suffix = path.suffix.casefold()
    if suffix in XLSX_EXTENSIONS:
        return load_xlsx_portfolio(path)
    if suffix in LEGACY_EXCEL_EXTENSIONS:
        raise InvestmentManagerError(
            "Legacy .xls import is not supported. Save the workbook as .xlsx first, or use conversion to CSV or JSON."
        )
    if suffix in JSON_EXTENSIONS:
        return load_json_portfolio(path)
    if suffix in CSV_EXTENSIONS or suffix == "":
        return load_csv_portfolio(path)
    raise InvestmentManagerError(f"Unsupported portfolio file type: {suffix}")


def load_json_portfolio(path: Path) -> list[Holding]:
    try:
        payload = json.loads(_read_portfolio_source_text(path, encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise InvestmentManagerError(f"Invalid JSON portfolio: {exc}") from exc
    if isinstance(payload, dict):
        rows = payload.get("holdings") or payload.get("positions") or payload.get("data")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise InvestmentManagerError("JSON portfolio must be a list or contain holdings.")
    return rows_to_holdings(rows)


def load_csv_portfolio(path: Path) -> list[Holding]:
    text = _read_portfolio_source_text(path, encoding="utf-8-sig")
    if not text.strip():
        raise InvestmentManagerError("Portfolio file is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.casefold() == ".tsv" else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    rows = list(reader)
    if not reader.fieldnames:
        raise InvestmentManagerError("Portfolio CSV has no header row.")
    return rows_to_holdings(rows)


def load_xlsx_portfolio(path: Path) -> list[Holding]:
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(
            f"Invalid Excel workbook: {exc}. Use a valid .xlsx file, or use conversion to CSV or JSON."
        ) from exc
    sheets = workbook_scan.get("sheets", [])
    if not any(sheet.get("row_count", 0) for sheet in sheets):
        raise InvestmentManagerError("Excel workbook has no rows.")

    usable_sheets = [
        sheet
        for sheet in sheets
        if sheet.get("usable") and isinstance(sheet.get("rows"), list)
    ]
    if not usable_sheets:
        scanned = ", ".join(
            f"{sheet.get('sheet_name', 'Sheet')}({sheet.get('row_count', 0)} rows)"
            for sheet in sheets
        )
        raise InvestmentManagerError(
            "Excel workbook has no recognizable header row. Required column: symbol / 代號."
            + (f" Scanned sheets: {scanned}." if scanned else "")
        )

    errors: list[str] = []
    for sheet in sorted(
        usable_sheets,
        key=lambda item: int(item.get("score", 0)),
        reverse=True,
    ):
        records = xlsx_records_from_rows(
            sheet["rows"],
            int(sheet["header_row_index"]),
            [str(header) for header in sheet.get("headers", [])],
        )
        try:
            return rows_to_holdings(records)
        except InvestmentManagerError as exc:
            errors.append(f"{sheet.get('sheet_name', 'Sheet')}: {exc}")

    detail = "; ".join(errors) if errors else "No usable data rows."
    raise InvestmentManagerError(f"Excel workbook has no usable holdings after scanning. {detail}")
