"""XLSX consolidated report layout: detection, parsing, and loading."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .portfolio_constants import XLSX_HEADER_SCAN_MAX_COLUMNS
from .portfolio_models import Holding, InvestmentManagerError
from .portfolio_utils import (
    normalize_header,
    normalize_market,
    normalize_symbol,
    parse_float,
)
from .portfolio_xlsx_inference import looks_like_portfolio_symbol
from .portfolio_xlsx_matrix import (
    xlsx_fund_currency,
    xlsx_horizontal_asset_type,
    xlsx_matrix_label,
)
from .portfolio_xlsx_scan import (
    public_xlsx_sheet_scan,
    scan_xlsx_workbook,
)


def xlsx_consolidated_report_preview_from_scan(
    workbook_scan: dict[str, Any],
) -> dict[str, Any]:
    candidates: list[tuple[dict[str, Any], int, int]] = []
    for sheet in workbook_scan.get("sheets", []):
        if not isinstance(sheet, dict) or not isinstance(sheet.get("rows"), list):
            continue
        rows = sheet["rows"]
        fund_header_index = None
        security_header_index = None
        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            name_header = xlsx_matrix_label(row[2] if len(row) > 2 else "")
            quantity_header = xlsx_matrix_label(row[13] if len(row) > 13 else "")
            price_header = xlsx_matrix_label(row[18] if len(row) > 18 else "")
            if name_header != "名稱":
                continue
            if quantity_header in {"總單位數", "單位數", "總股數"}:
                fund_header_index = index
            elif price_header in {"即時股價", "股價", "現價"}:
                security_header_index = index
        if fund_header_index is not None and security_header_index is not None:
            candidates.append((sheet, fund_header_index, security_header_index))

    if not candidates:
        return {
            "detected": False,
            "recommended": False,
            "layout": "consolidated_report",
            "holding_count": 0,
        }
    sheet, fund_header_index, security_header_index = sorted(
        candidates,
        key=lambda item: (
            str(item[0].get("sheet_name") or "").strip() == "報酬",
            len(item[0].get("rows") or []),
        ),
        reverse=True,
    )[0]
    rows = sheet["rows"]
    data_end_row_number = len(rows)
    for index in range(security_header_index + 1, len(rows)):
        next_rows = rows[index : index + 3]
        if len(next_rows) < 3:
            continue
        if all(
            not str(row[2] if isinstance(row, list) and len(row) > 2 else "").strip()
            for row in next_rows
        ):
            data_end_row_number = index
            break
    config: dict[str, Any] = {
        "layout": "consolidated_report",
        "sheet_name": str(sheet.get("sheet_name") or ""),
        "fund_header_row_number": fund_header_index + 1,
        "fund_start_row_number": fund_header_index + 2,
        "security_header_row_number": security_header_index + 1,
        "security_start_row_number": security_header_index + 2,
        "data_end_row_number": data_end_row_number,
        "tw_symbol_column_index": 1,
        "fund_name_column_index": 2,
        "tw_name_column_index": 2,
        "us_symbol_column_index": 2,
        "us_name_column_index": 1,
        "dividend_amount_column_index": 3,
        "dividend_per_unit_column_index": 4,
        "monthly_dividend_column_index": 5,
        "annual_dividend_yield_column_index": 6,
        "payback_rate_column_index": 7,
        "quantity_column_index": 13,
        "cost_amount_column_index": 14,
        "price_column_index": 18,
        "current_value_column_index": 21,
        "include_zero_quantity": True,
        "fund_market": "FUND",
        "fund_asset_type": "FUND",
        "fund_currency": "TWD",
        "fund_principal_currency": "TWD",
        "tw_market": "TW",
        "tw_asset_type": "AUTO",
        "tw_currency": "TWD",
        "tw_principal_currency": "TWD",
        "us_market": "US",
        "us_asset_type": "AUTO",
        "us_currency": "USD",
        "us_principal_currency": "TWD",
    }
    holdings, details = xlsx_holdings_from_consolidated_report(sheet, config)
    return {
        **config,
        "detected": bool(holdings),
        "recommended": bool(holdings),
        "holding_count": len(holdings),
        "fund_count": sum(holding.asset_type == "FUND" for holding in holdings),
        "tw_count": sum(holding.market == "TW" for holding in holdings),
        "us_count": sum(holding.market == "US" for holding in holdings),
        "etf_count": sum(holding.asset_type == "ETF" for holding in holdings),
        "stock_count": sum(holding.asset_type == "STOCK" for holding in holdings),
        "skipped_row_count": details["skipped_row_count"],
        "sample_holdings": [
            {
                "symbol": holding.symbol,
                "name": holding.name,
                "market": holding.market,
                "asset_type": holding.asset_type,
                "quantity": holding.quantity,
                "average_cost": holding.average_cost,
                "currency": holding.currency,
                "principal_amount": holding.principal_amount,
                "principal_currency": holding.principal_currency,
                "principal_twd": holding.principal_twd,
                "annual_dividend_yield_percent": holding.annual_dividend_yield_percent,
                "estimated_weekly_dividend_twd": holding.estimated_weekly_dividend_twd,
            }
            for holding in (
                holdings[:3]
                + [item for item in holdings if item.market == "TW"][:3]
                + [item for item in holdings if item.market == "US"][:4]
            )
        ],
    }


def xlsx_report_integer_setting(
    config: dict[str, Any],
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(config.get(field))
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError(f"Invalid {field} in consolidated Excel mapping.") from exc
    if value < minimum or value > maximum:
        raise InvestmentManagerError(
            f"{field} must be between {minimum} and {maximum}."
        )
    return value


def xlsx_report_asset_type(
    symbol: str,
    name: str,
    market: str,
    configured: str,
) -> str:
    inferred = xlsx_horizontal_asset_type(symbol, market, configured)
    if inferred == "ETF" or configured not in {"", "AUTO"}:
        return inferred
    normalized_name = normalize_header(name)
    if market == "US" and any(
        token in normalized_name
        for token in ("ETF", "期權收益", "收益策略", "收益增強", "YIELDMAX")
    ):
        return "ETF"
    return inferred


def xlsx_report_principal_currency(
    config: dict[str, Any],
    prefix: str,
    asset_currency: str,
) -> str:
    configured = str(
        config.get(f"{prefix}_principal_currency") or "TWD"
    ).strip().upper()
    if configured in {"AUTO", "ASSET"}:
        return asset_currency
    if not configured or len(configured) > 8 or not configured.replace("-", "").isalnum():
        raise InvestmentManagerError(
            f"Invalid {prefix}_principal_currency in consolidated Excel mapping."
        )
    return configured


def xlsx_holdings_from_consolidated_report(
    sheet: dict[str, Any],
    raw_config: dict[str, Any],
) -> tuple[list[Holding], dict[str, int]]:
    rows = sheet.get("rows")
    if not isinstance(rows, list) or not rows:
        raise InvestmentManagerError("Consolidated Excel worksheet has no rows.")
    config = dict(raw_config)
    row_count = len(rows)
    fund_start = xlsx_report_integer_setting(
        config, "fund_start_row_number", minimum=1, maximum=row_count
    )
    security_start = xlsx_report_integer_setting(
        config, "security_start_row_number", minimum=1, maximum=row_count
    )
    data_end = xlsx_report_integer_setting(
        config, "data_end_row_number", minimum=max(fund_start, security_start), maximum=row_count
    )
    column_fields = (
        "tw_symbol_column_index",
        "fund_name_column_index",
        "tw_name_column_index",
        "us_symbol_column_index",
        "us_name_column_index",
        "dividend_amount_column_index",
        "dividend_per_unit_column_index",
        "monthly_dividend_column_index",
        "annual_dividend_yield_column_index",
        "payback_rate_column_index",
        "quantity_column_index",
        "cost_amount_column_index",
        "price_column_index",
        "current_value_column_index",
    )
    columns = {
        field: xlsx_report_integer_setting(
            config,
            field,
            minimum=0,
            maximum=XLSX_HEADER_SCAN_MAX_COLUMNS - 1,
        )
        for field in column_fields
    }
    include_zero = bool(config.get("include_zero_quantity", True))
    holdings: list[Holding] = []
    skipped = 0
    for excel_row_number in range(fund_start, data_end + 1):
        row = rows[excel_row_number - 1]
        if not isinstance(row, list):
            skipped += 1
            continue

        def cell(field: str) -> str:
            index = columns[field]
            return str(row[index] if index < len(row) else "").strip()

        quantity = parse_float(cell("quantity_column_index")) or 0.0
        principal_amount = parse_float(cell("cost_amount_column_index"))
        dividend_amount_twd = parse_float(cell("dividend_amount_column_index"))
        dividend_per_unit = parse_float(cell("dividend_per_unit_column_index"))
        monthly_dividend_twd = parse_float(cell("monthly_dividend_column_index"))
        annual_yield = parse_float(cell("annual_dividend_yield_column_index"))
        payback_rate = parse_float(cell("payback_rate_column_index"))
        annual_yield_percent = (
            annual_yield * 100.0
            if annual_yield is not None and abs(annual_yield) <= 1
            else annual_yield
        )
        payback_rate_percent = (
            payback_rate * 100.0
            if payback_rate is not None and abs(payback_rate) <= 1
            else payback_rate
        )
        current_value_twd = parse_float(cell("current_value_column_index"))
        estimated_annual_dividend_twd = None
        if monthly_dividend_twd is not None and monthly_dividend_twd > 0:
            estimated_annual_dividend_twd = monthly_dividend_twd * 12.0
        elif (
            current_value_twd is not None
            and current_value_twd > 0
            and annual_yield_percent is not None
            and annual_yield_percent > 0
        ):
            estimated_annual_dividend_twd = (
                current_value_twd * annual_yield_percent / 100.0
            )
        estimated_weekly_dividend_twd = (
            estimated_annual_dividend_twd / 52.0
            if estimated_annual_dividend_twd is not None
            else None
        )
        if not include_zero and quantity <= 0:
            skipped += 1
            continue
        if excel_row_number < security_start:
            name = cell("fund_name_column_index")
            if not name or xlsx_matrix_label(name) in {"名稱", "投資組合", "現金"}:
                skipped += 1
                continue
            market = normalize_market(str(config.get("fund_market") or "FUND")) or "FUND"
            asset_type = str(config.get("fund_asset_type") or "FUND").strip().upper()
            currency = xlsx_fund_currency(
                name,
                str(config.get("fund_currency") or "TWD").strip().upper(),
            )
            principal_currency = xlsx_report_principal_currency(
                config, "fund", currency
            )
            symbol = f"FUND-R{excel_row_number:03d}"
        else:
            raw_tw_symbol = cell("tw_symbol_column_index")
            raw_us_symbol = cell("us_symbol_column_index")
            if xlsx_matrix_label(raw_tw_symbol) in {"現金", "名稱", "合計", "總計"}:
                skipped += 1
                continue
            if re.fullmatch(r"\d{4,6}[A-Z]{0,2}", normalize_symbol(raw_tw_symbol)):
                symbol = normalize_symbol(raw_tw_symbol)
                name = cell("tw_name_column_index")
                if not name:
                    skipped += 1
                    continue
                market = normalize_market(str(config.get("tw_market") or "TW")) or "TW"
                configured_type = str(config.get("tw_asset_type") or "AUTO").strip().upper()
                asset_type = xlsx_report_asset_type(symbol, name, market, configured_type)
                currency = str(config.get("tw_currency") or "TWD").strip().upper()
                principal_currency = xlsx_report_principal_currency(
                    config, "tw", currency
                )
            else:
                symbol = normalize_symbol(raw_us_symbol)
                if not looks_like_portfolio_symbol(symbol):
                    skipped += 1
                    continue
                name = cell("us_name_column_index")
                market = normalize_market(str(config.get("us_market") or "US")) or "US"
                configured_type = str(config.get("us_asset_type") or "AUTO").strip().upper()
                asset_type = xlsx_report_asset_type(symbol, name, market, configured_type)
                currency = str(config.get("us_currency") or "USD").strip().upper()
                principal_currency = xlsx_report_principal_currency(
                    config, "us", currency
                )
        average_cost = (
            principal_amount / quantity
            if principal_currency == currency
            and principal_amount is not None
            and principal_amount > 0
            and quantity > 0
            else None
        )
        holdings.append(
            Holding(
                symbol=symbol,
                name=name,
                market=market,
                asset_type=asset_type,
                quantity=quantity,
                average_cost=average_cost,
                currency=currency,
                principal_amount=principal_amount,
                principal_currency=principal_currency,
                principal_twd=(
                    principal_amount if principal_currency == "TWD" else None
                ),
                source_row=excel_row_number,
                dividend_amount_twd=dividend_amount_twd,
                dividend_per_unit=dividend_per_unit,
                monthly_dividend_twd=monthly_dividend_twd,
                annual_dividend_yield_percent=annual_yield_percent,
                payback_rate_percent=payback_rate_percent,
                current_value_twd=current_value_twd,
                estimated_annual_dividend_twd=estimated_annual_dividend_twd,
                estimated_weekly_dividend_twd=estimated_weekly_dividend_twd,
            )
        )
    return holdings, {"skipped_row_count": skipped}


def load_xlsx_portfolio_consolidated_report(
    path: Path,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[list[Holding], dict[str, Any]]:
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc
    detected = xlsx_consolidated_report_preview_from_scan(workbook_scan)
    if not detected.get("detected"):
        raise InvestmentManagerError("No consolidated return worksheet was detected.")
    merged_config = {
        key: value
        for key, value in detected.items()
        if key
        not in {
            "detected",
            "recommended",
            "holding_count",
            "fund_count",
            "tw_count",
            "us_count",
            "etf_count",
            "stock_count",
            "skipped_row_count",
            "sample_holdings",
        }
    }
    if isinstance(config, dict):
        merged_config.update(config)
    sheet_name = str(merged_config.get("sheet_name") or "")
    sheet = next(
        (
            item
            for item in workbook_scan.get("sheets", [])
            if isinstance(item, dict)
            and str(item.get("sheet_name") or "").strip() == sheet_name.strip()
        ),
        None,
    )
    if sheet is None:
        raise InvestmentManagerError(f"Consolidated worksheet not found: {sheet_name}")
    merged_config["sheet_name"] = str(sheet.get("sheet_name") or sheet_name)
    sheet_name = merged_config["sheet_name"]
    holdings, parse_details = xlsx_holdings_from_consolidated_report(sheet, merged_config)
    if not holdings:
        raise InvestmentManagerError(
            "Consolidated Excel settings produced no holdings. Check rows and columns."
        )
    selected_sheet = {
        **public_xlsx_sheet_scan(sheet),
        "usable": True,
        "score": 300,
        "header_mode": "consolidated_report",
        "header_depth": 1,
        "header_row_index": int(merged_config["security_header_row_number"]) - 1,
        "header_row_number": merged_config["security_header_row_number"],
        "data_start_row_index": int(merged_config["fund_start_row_number"]) - 1,
        "data_start_row_number": merged_config["fund_start_row_number"],
        "valid_data_row_count": len(holdings),
        "consolidated_mapping": merged_config,
    }
    public_sheets = [
        selected_sheet
        if str(item.get("sheet_name") or "") == sheet_name
        else public_xlsx_sheet_scan(item)
        for item in workbook_scan.get("sheets", [])
        if isinstance(item, dict)
    ]
    profile = {"layout": "consolidated_report", **merged_config}
    return holdings, {
        "profile": profile,
        "workbook_scan": {
            "sheet_count": len(public_sheets),
            "selected_sheet": selected_sheet,
            "sheets": public_sheets,
        },
        "imported_row_count": len(holdings),
        "skipped_row_count": parse_details["skipped_row_count"],
    }
