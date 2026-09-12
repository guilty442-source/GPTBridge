"""XLSX horizontal matrix layout: detection, parsing, and loading."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .portfolio_constants import (
    XLSX_HEADER_SCAN_MAX_COLUMNS,
    XLSX_HORIZONTAL_GROUP_WIDTH,
)
from .portfolio_models import Holding, InvestmentManagerError
from .portfolio_utils import (
    infer_currency,
    normalize_market,
    normalize_symbol,
)
from .portfolio_xlsx_inference import looks_like_portfolio_symbol
from .portfolio_xlsx_lowlevel import xlsx_column_letter
from .portfolio_xlsx_matrix import (
    xlsx_fund_currency,
    xlsx_horizontal_asset_type,
    xlsx_horizontal_sheet_defaults,
    xlsx_horizontal_sheet_is_summary,
    xlsx_matrix_group_starts,
    xlsx_matrix_header_row,
    xlsx_matrix_label,
    xlsx_matrix_numeric_value,
    xlsx_matrix_related_row,
)
from .portfolio_xlsx_scan import (
    public_xlsx_sheet_scan,
    scan_xlsx_workbook,
)


def xlsx_horizontal_matrix_preview_from_scan(
    workbook_scan: dict[str, Any],
) -> dict[str, Any]:
    detected_sheets: list[dict[str, Any]] = []
    for sheet in workbook_scan.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        detected = detect_xlsx_horizontal_matrix_sheet(sheet)
        if detected is not None:
            detected_sheets.append(detected)

    preferred_names = {"股票", "美股", "平台基金", "銀行基金"}
    preferred_detected = {
        str(sheet.get("sheet_name") or "").strip()
        for sheet in detected_sheets
        if str(sheet.get("sheet_name") or "").strip() in preferred_names
    }
    if preferred_detected:
        for sheet in detected_sheets:
            sheet["selected_by_default"] = (
                str(sheet.get("sheet_name") or "").strip() in preferred_detected
            )
    else:
        for sheet in detected_sheets:
            sheet["selected_by_default"] = not xlsx_horizontal_sheet_is_summary(
                str(sheet.get("sheet_name") or "")
            )

    selected_sheets = [
        sheet for sheet in detected_sheets if sheet.get("selected_by_default")
    ]
    if not selected_sheets and detected_sheets:
        detected_sheets[0]["selected_by_default"] = True
        selected_sheets = [detected_sheets[0]]
    return {
        "detected": bool(detected_sheets),
        "recommended": bool(selected_sheets),
        "layout": "horizontal_matrix",
        "sheet_count": len(detected_sheets),
        "selected_sheet_count": len(selected_sheets),
        "holding_count": sum(
            int(sheet.get("holding_count") or 0) for sheet in selected_sheets
        ),
        "supported_asset_classes": ["ETF", "台股", "美股", "共同基金"],
        "sheets": detected_sheets,
    }


def detect_xlsx_horizontal_matrix_sheet(
    sheet: dict[str, Any],
) -> dict[str, Any] | None:
    rows = sheet.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    for holding_row_index in range(len(rows) - 1, -1, -1):
        row = rows[holding_row_index]
        if not isinstance(row, list):
            continue
        group_starts = xlsx_matrix_group_starts(row, {"持有", "持倉"})
        if not group_starts:
            continue
        quantity_row_index = xlsx_matrix_related_row(
            rows,
            holding_row_index,
            group_starts,
            {"股數", "單位數", "持有股數", "持有單位"},
        )
        if quantity_row_index is None:
            continue
        header_row_index = xlsx_matrix_header_row(
            rows,
            holding_row_index,
            group_starts,
        )
        if header_row_index is None:
            continue
        price_row_index = xlsx_matrix_related_row(
            rows,
            holding_row_index,
            group_starts,
            {"股價", "淨值", "價格", "現價"},
        )
        defaults = xlsx_horizontal_sheet_defaults(str(sheet.get("sheet_name") or ""))
        config = {
            "sheet_index": int(sheet.get("sheet_index") or 0),
            "sheet_name": str(sheet.get("sheet_name") or ""),
            "header_row_number": header_row_index + 1,
            "holding_row_number": holding_row_index + 1,
            "price_row_number": price_row_index + 1 if price_row_index is not None else None,
            "quantity_row_number": quantity_row_index + 1,
            "first_asset_column_index": group_starts[0],
            "first_asset_column_letter": xlsx_column_letter(group_starts[0]),
            "group_width": XLSX_HORIZONTAL_GROUP_WIDTH,
            **defaults,
        }
        holdings, skipped = xlsx_holdings_from_horizontal_sheet(sheet, config)
        if not holdings:
            continue
        return {
            **config,
            "detected": True,
            "selected_by_default": False,
            "holding_count": len(holdings),
            "skipped_group_count": skipped,
            "sample_holdings": [
                {
                    "symbol": holding.symbol,
                    "name": holding.name,
                    "market": holding.market,
                    "asset_type": holding.asset_type,
                    "quantity": holding.quantity,
                    "average_cost": holding.average_cost,
                    "currency": holding.currency,
                }
                for holding in holdings[:8]
            ],
        }
    return None


def normalize_xlsx_horizontal_sheet_config(
    raw_config: dict[str, Any],
    detected: dict[str, Any],
) -> dict[str, Any]:
    config = {**detected, **raw_config}
    sheet_name = str(config.get("sheet_name") or "").strip()
    if not sheet_name:
        raise InvestmentManagerError("Horizontal Excel mapping requires a worksheet name.")

    def positive_row(field: str, allow_blank: bool = False) -> int | None:
        raw_value = config.get(field)
        if allow_blank and (raw_value is None or raw_value == ""):
            return None
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise InvestmentManagerError(f"Invalid {field} for {sheet_name}.") from exc
        if value < 1:
            raise InvestmentManagerError(f"{field} must be a positive integer for {sheet_name}.")
        return value

    header_row_number = positive_row("header_row_number")
    holding_row_number = positive_row("holding_row_number")
    price_row_number = positive_row("price_row_number", allow_blank=True)
    quantity_row_number = positive_row("quantity_row_number")
    try:
        first_asset_column_index = int(config.get("first_asset_column_index"))
        group_width = int(config.get("group_width") or XLSX_HORIZONTAL_GROUP_WIDTH)
    except (TypeError, ValueError) as exc:
        raise InvestmentManagerError(f"Invalid horizontal column settings for {sheet_name}.") from exc
    if first_asset_column_index < 0 or first_asset_column_index >= XLSX_HEADER_SCAN_MAX_COLUMNS:
        raise InvestmentManagerError(f"Invalid first asset column for {sheet_name}.")
    if group_width < 2 or group_width > 12:
        raise InvestmentManagerError(f"Group width must be between 2 and 12 for {sheet_name}.")
    market = normalize_market(str(config.get("market") or detected.get("market") or ""))
    asset_type = str(config.get("asset_type") or detected.get("asset_type") or "AUTO").strip().upper()
    currency = str(config.get("currency") or detected.get("currency") or "").strip().upper()
    return {
        "sheet_name": sheet_name,
        "header_row_number": header_row_number,
        "holding_row_number": holding_row_number,
        "price_row_number": price_row_number,
        "quantity_row_number": quantity_row_number,
        "first_asset_column_index": first_asset_column_index,
        "group_width": group_width,
        "market": market,
        "asset_type": asset_type,
        "currency": currency,
        "category_label": str(config.get("category_label") or detected.get("category_label") or ""),
    }


def xlsx_holdings_from_horizontal_sheet(
    sheet: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[Holding], int]:
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        return [], 0
    header_index = int(config.get("header_row_number") or 0) - 1
    holding_index = int(config.get("holding_row_number") or 0) - 1
    quantity_index = int(config.get("quantity_row_number") or 0) - 1
    price_number = config.get("price_row_number")
    price_index = int(price_number) - 1 if price_number not in (None, "") else None
    required_indexes = [header_index, holding_index, quantity_index]
    if any(index < 0 or index >= len(rows) for index in required_indexes):
        raise InvestmentManagerError(
            f"Horizontal Excel row settings are outside worksheet {config.get('sheet_name')}."
        )
    if price_index is not None and (price_index < 0 or price_index >= len(rows)):
        raise InvestmentManagerError(
            f"Price row is outside worksheet {config.get('sheet_name')}."
        )
    header_row = rows[header_index]
    holding_row = rows[holding_index]
    quantity_row = rows[quantity_index]
    price_row = rows[price_index] if price_index is not None else []
    first_column = int(config.get("first_asset_column_index") or 0)
    group_width = int(config.get("group_width") or XLSX_HORIZONTAL_GROUP_WIDTH)
    market = normalize_market(str(config.get("market") or "")) or "TW"
    configured_type = str(config.get("asset_type") or "AUTO").strip().upper()
    default_currency = str(config.get("currency") or infer_currency(market)).strip().upper()
    sheet_index = int(sheet.get("sheet_index") or 0)
    holdings: list[Holding] = []
    skipped = 0
    for start in range(first_column, len(header_row), group_width):
        raw_label = str(header_row[start] if start < len(header_row) else "").strip()
        if not raw_label or xlsx_matrix_label(raw_label) in {"合計", "總計", "小計"}:
            skipped += 1
            continue
        quantity = xlsx_matrix_numeric_value(quantity_row, start, group_width)
        if quantity is None or quantity <= 0:
            skipped += 1
            continue
        holding_amount = xlsx_matrix_numeric_value(holding_row, start, group_width)
        current_price = xlsx_matrix_numeric_value(price_row, start, group_width)
        if market == "FUND" or configured_type == "FUND":
            symbol = f"FUND-{sheet_index + 1:02d}-{xlsx_column_letter(start)}"
            name = raw_label
        else:
            symbol = normalize_symbol(raw_label)
            name = ""
            if not looks_like_portfolio_symbol(symbol):
                skipped += 1
                continue
        asset_type = xlsx_horizontal_asset_type(symbol, market, configured_type)
        currency = (
            xlsx_fund_currency(raw_label, default_currency)
            if asset_type == "FUND"
            else default_currency or infer_currency(market)
        )
        average_cost: float | None = None
        if holding_amount is not None and holding_amount > 0 and quantity > 0:
            if market == "TW" and currency == "TWD":
                average_cost = holding_amount / quantity
            elif asset_type == "FUND" and currency == "TWD":
                average_cost = holding_amount / quantity
        if average_cost is None and current_price is not None and current_price > 0:
            # Keep the latest workbook value available for local-only funds without
            # pretending it is a broker-confirmed historical cost basis.
            average_cost = current_price if asset_type == "FUND" else None
        holdings.append(
            Holding(
                symbol=symbol,
                name=name,
                market=market,
                asset_type=asset_type,
                quantity=quantity,
                average_cost=average_cost,
                currency=currency,
                principal_amount=holding_amount,
                principal_currency=currency,
                principal_twd=(holding_amount if currency == "TWD" else None),
                source_row=quantity_index + 1,
            )
        )
    return holdings, skipped


def merge_xlsx_horizontal_holdings(holdings: list[Holding]) -> list[Holding]:
    merged: dict[tuple[str, str], Holding] = {}
    for holding in holdings:
        key = (holding.market, holding.symbol)
        previous = merged.get(key)
        if previous is None:
            merged[key] = holding
            continue
        quantity = previous.quantity + holding.quantity
        average_cost = None
        if (
            previous.average_cost is not None
            and holding.average_cost is not None
            and quantity > 0
        ):
            average_cost = (
                previous.average_cost * previous.quantity
                + holding.average_cost * holding.quantity
            ) / quantity
        merged[key] = Holding(
            symbol=holding.symbol,
            name=previous.name or holding.name,
            market=holding.market,
            asset_type=(
                "ETF"
                if "ETF" in {previous.asset_type, holding.asset_type}
                else previous.asset_type or holding.asset_type
            ),
            quantity=quantity,
            average_cost=average_cost,
            currency=previous.currency or holding.currency,
            principal_amount=(
                (previous.principal_amount or 0) + (holding.principal_amount or 0)
                if previous.principal_amount is not None
                or holding.principal_amount is not None
                else None
            ),
            principal_currency=previous.principal_currency or holding.principal_currency,
            principal_twd=(
                (previous.principal_twd or 0) + (holding.principal_twd or 0)
                if previous.principal_twd is not None or holding.principal_twd is not None
                else None
            ),
            source_row=previous.source_row,
        )
    return list(merged.values())


def load_xlsx_portfolio_horizontal_matrix(
    path: Path,
    *,
    sheet_configs: list[dict[str, Any]] | None = None,
) -> tuple[list[Holding], dict[str, Any]]:
    try:
        workbook_scan = scan_xlsx_workbook(path, include_rows=True)
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise InvestmentManagerError(f"Invalid Excel workbook: {exc}") from exc
    preview = xlsx_horizontal_matrix_preview_from_scan(workbook_scan)
    detected_by_name = {
        str(sheet.get("sheet_name") or ""): sheet
        for sheet in preview.get("sheets", [])
        if isinstance(sheet, dict)
    }
    if not detected_by_name:
        raise InvestmentManagerError("No horizontal holding worksheets were detected.")
    raw_configs = sheet_configs if isinstance(sheet_configs, list) else []
    if raw_configs:
        selected_configs = [
            config
            for config in raw_configs
            if isinstance(config, dict) and config.get("enabled", True)
        ]
    else:
        selected_configs = [
            sheet
            for sheet in preview.get("sheets", [])
            if isinstance(sheet, dict) and sheet.get("selected_by_default")
        ]
    if not selected_configs:
        raise InvestmentManagerError("Select at least one horizontal holding worksheet.")

    workbook_sheets = {
        str(sheet.get("sheet_name") or ""): sheet
        for sheet in workbook_scan.get("sheets", [])
        if isinstance(sheet, dict)
    }
    normalized_configs: list[dict[str, Any]] = []
    all_holdings: list[Holding] = []
    skipped_count = 0
    selected_scan_sheets: list[dict[str, Any]] = []
    for raw_config in selected_configs:
        sheet_name = str(raw_config.get("sheet_name") or "").strip()
        detected = detected_by_name.get(sheet_name)
        sheet = workbook_sheets.get(sheet_name)
        if detected is None or sheet is None:
            raise InvestmentManagerError(f"Horizontal worksheet not found: {sheet_name}")
        config = normalize_xlsx_horizontal_sheet_config(raw_config, detected)
        sheet_holdings, skipped = xlsx_holdings_from_horizontal_sheet(sheet, config)
        if not sheet_holdings:
            raise InvestmentManagerError(
                f"Horizontal settings produced no holdings for worksheet {sheet_name}."
            )
        normalized_configs.append(config)
        all_holdings.extend(sheet_holdings)
        skipped_count += skipped
        selected_scan_sheets.append(
            {
                **public_xlsx_sheet_scan(sheet),
                "usable": True,
                "score": 260,
                "header_mode": "horizontal_matrix",
                "header_depth": 1,
                "header_row_index": int(config["header_row_number"]) - 1,
                "header_row_number": config["header_row_number"],
                "data_start_row_index": int(config["quantity_row_number"]) - 1,
                "data_start_row_number": config["quantity_row_number"],
                "valid_data_row_count": len(sheet_holdings),
                "horizontal_mapping": config,
            }
        )
    holdings = merge_xlsx_horizontal_holdings(all_holdings)
    selected_name = "、".join(config["sheet_name"] for config in normalized_configs)
    selected_summary = {
        "sheet_name": selected_name,
        "usable": True,
        "score": 260,
        "header_mode": "horizontal_matrix",
        "header_depth": 1,
        "header_row_number": normalized_configs[0]["header_row_number"],
        "data_start_row_number": normalized_configs[0]["quantity_row_number"],
        "valid_data_row_count": len(holdings),
        "source_sheet_count": len(normalized_configs),
    }
    public_sheets = []
    selected_by_name = {
        str(sheet.get("sheet_name") or ""): sheet for sheet in selected_scan_sheets
    }
    for sheet in workbook_scan.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        sheet_name = str(sheet.get("sheet_name") or "")
        public_sheets.append(selected_by_name.get(sheet_name, public_xlsx_sheet_scan(sheet)))
    profile = {
        "layout": "horizontal_matrix",
        "sheets": normalized_configs,
    }
    return holdings, {
        "profile": profile,
        "workbook_scan": {
            "sheet_count": len(public_sheets),
            "selected_sheet": selected_summary,
            "sheets": public_sheets,
        },
        "imported_row_count": len(holdings),
        "source_position_count": len(all_holdings),
        "merged_position_count": max(0, len(all_holdings) - len(holdings)),
        "skipped_row_count": skipped_count,
    }
