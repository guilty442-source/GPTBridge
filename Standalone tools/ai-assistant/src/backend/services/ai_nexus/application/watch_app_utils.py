from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..infrastructure import portfolio_file as investment_manager_core


def local_device_now() -> datetime:
    return datetime.now(timezone.utc)


class WatchAppUtilsMixin:
    """Static utility helpers for InvestmentWatchService."""

    @staticmethod
    def _assert_import_symbol_quality(holdings: list[dict[str, Any]]) -> None:
        symbols = [str(item.get("symbol") or "").strip() for item in holdings]
        invalid_count = sum(
            1
            for symbol in symbols
            if not investment_manager_core.looks_like_portfolio_symbol(symbol)
        )
        invalid_ratio = invalid_count / len(symbols) if symbols else 0.0
        decimal_count = sum(
            1
            for symbol in symbols
            if re.fullmatch(
                r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[Ee][+-]?\d+)?",
                symbol,
            )
        )
        if decimal_count >= 3 or (invalid_count >= 3 and invalid_ratio >= 0.2):
            raise investment_manager_core.InvestmentManagerError(
                f"Excel 欄位映射異常：{len(symbols)} 筆中有 {invalid_count} 筆無效代號；"
                "已取消匯入並保留原有資料，請檢查代號、名稱與數量欄位。"
            )

    @staticmethod
    def _age_hours(value: Any) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_device_now().tzinfo)
        delta = local_device_now() - parsed.astimezone()
        return round(max(0.0, delta.total_seconds() / 3600), 2)

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _holding_to_dict(holding: Any) -> dict[str, Any]:
        return {
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
            "source_row": holding.source_row,
            "dividend_amount_twd": holding.dividend_amount_twd,
            "dividend_per_unit": holding.dividend_per_unit,
            "monthly_dividend_twd": holding.monthly_dividend_twd,
            "annual_dividend_yield_percent": holding.annual_dividend_yield_percent,
            "payback_rate_percent": holding.payback_rate_percent,
            "current_value_twd": holding.current_value_twd,
            "estimated_annual_dividend_twd": holding.estimated_annual_dividend_twd,
            "estimated_weekly_dividend_twd": holding.estimated_weekly_dividend_twd,
        }
