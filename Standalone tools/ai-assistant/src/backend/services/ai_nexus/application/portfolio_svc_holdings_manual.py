from __future__ import annotations

from typing import Any

from ..infrastructure.analytics_repository import number


_MANUAL_FREQUENCY_DEFINITIONS = {
    "unknown": ("待確認", None),
    "none": ("無配息", 0),
    "weekly": ("每週", 52),
    "biweekly": ("每兩週", 26),
    "monthly": ("每月", 12),
    "bimonthly": ("每兩月", 6),
    "quarterly": ("每季", 4),
    "semiannual": ("每半年", 2),
    "annual": ("每年", 1),
    "irregular": ("不定期", None),
}


class PortfolioSvcHoldingsManualMixin:
    """Manual holding normalization, validation, and dividend estimation."""

    @staticmethod
    def _normalized_manual_holding(
        payload: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        market = str(payload.get("market") or "").strip().upper()
        asset_type = str(payload.get("asset_type") or "STOCK").strip().upper()
        currency = str(payload.get("currency") or "TWD").strip().upper()
        quantity = number(payload.get("quantity"), -1)
        average_cost = number(payload.get("average_cost"), -1)
        optional = PortfolioSvcHoldingsManualMixin._manual_optional_fields(
            payload, existing, currency
        )
        frequency = PortfolioSvcHoldingsManualMixin._manual_dividend_frequency(
            payload, existing
        )
        fund = PortfolioSvcHoldingsManualMixin._manual_fund_identity(payload, existing)
        estimates = PortfolioSvcHoldingsManualMixin._manual_dividend_estimates(
            optional, frequency, quantity
        )
        principal_amount, principal_twd = (
            PortfolioSvcHoldingsManualMixin._manual_validated_principal(
                symbol, market, currency, quantity, average_cost, optional
            )
        )
        return PortfolioSvcHoldingsManualMixin._manual_holding_dict(
            payload,
            existing,
            {
                "symbol": symbol,
                "market": market,
                "asset_type": asset_type,
                "currency": currency,
                "quantity": quantity,
                "average_cost": average_cost,
                "principal_amount": principal_amount,
                "principal_twd": principal_twd,
                **optional,
                **frequency,
                **fund,
                **estimates,
            },
        )

    @staticmethod
    def _optional_number(
        payload: dict[str, Any],
        existing: dict[str, Any] | None,
        field: str,
    ) -> float | None:
        raw_value = payload.get(field, (existing or {}).get(field))
        if raw_value is None or raw_value == "":
            return None
        parsed = number(raw_value, -1)
        if parsed < 0:
            raise ValueError(f"{field} 不可小於零")
        return parsed

    @staticmethod
    def _manual_optional_fields(
        payload: dict[str, Any],
        existing: dict[str, Any] | None,
        currency: str,
    ) -> dict[str, Any]:
        optional = PortfolioSvcHoldingsManualMixin._optional_number
        return {
            "dividend_amount_twd": optional(payload, existing, "dividend_amount_twd"),
            "dividend_per_unit": optional(payload, existing, "dividend_per_unit"),
            "monthly_dividend_twd": optional(payload, existing, "monthly_dividend_twd"),
            "annual_dividend_yield_percent": optional(
                payload, existing, "annual_dividend_yield_percent"
            ),
            "payback_rate_percent": optional(payload, existing, "payback_rate_percent"),
            "current_value_twd": optional(payload, existing, "current_value_twd"),
            "principal_amount": optional(payload, existing, "principal_amount"),
            "principal_twd": optional(payload, existing, "principal_twd"),
            "principal_currency": str(
                payload.get("principal_currency")
                or (existing or {}).get("principal_currency")
                or currency
            ).strip().upper(),
        }

    @staticmethod
    def _manual_dividend_frequency(
        payload: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        dividend_frequency = str(
            payload.get(
                "dividend_frequency",
                (existing or {}).get("dividend_frequency") or "unknown",
            )
            or "unknown"
        ).strip().casefold()
        if dividend_frequency not in _MANUAL_FREQUENCY_DEFINITIONS:
            raise ValueError("配息頻率不在允許清單")
        label, per_year = _MANUAL_FREQUENCY_DEFINITIONS[dividend_frequency]
        return {
            "dividend_frequency": dividend_frequency,
            "dividend_frequency_label": label,
            "dividend_frequency_per_year": per_year,
        }

    @staticmethod
    def _manual_fund_identity(
        payload: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "fund_code": str(
                payload.get("fund_code") or (existing or {}).get("fund_code") or ""
            ).strip().upper(),
            "fund_isin": str(
                payload.get("fund_isin") or (existing or {}).get("fund_isin") or ""
            ).strip().upper(),
            "fund_share_class": str(
                payload.get("fund_share_class")
                or (existing or {}).get("fund_share_class")
                or ""
            ).strip(),
            "fund_quote_symbol": str(
                payload.get("fund_quote_symbol")
                or (existing or {}).get("fund_quote_symbol")
                or ""
            ).strip().upper(),
        }

    @staticmethod
    def _manual_dividend_estimates(
        optional: dict[str, Any],
        frequency: dict[str, Any],
        quantity: float,
    ) -> dict[str, Any]:
        monthly_dividend_twd = optional["monthly_dividend_twd"]
        dividend_per_unit = optional["dividend_per_unit"]
        current_value_twd = optional["current_value_twd"]
        annual_dividend_yield_percent = optional["annual_dividend_yield_percent"]
        dividend_frequency_per_year = frequency["dividend_frequency_per_year"]
        estimated_annual_dividend_twd = (
            0.0
            if frequency["dividend_frequency"] == "none"
            else monthly_dividend_twd * 12.0
            if monthly_dividend_twd is not None and monthly_dividend_twd > 0
            else dividend_per_unit * quantity * dividend_frequency_per_year
            if dividend_per_unit is not None
            and dividend_per_unit > 0
            and dividend_frequency_per_year is not None
            and dividend_frequency_per_year > 0
            else current_value_twd * annual_dividend_yield_percent / 100.0
            if current_value_twd is not None
            and annual_dividend_yield_percent is not None
            else None
        )
        estimated_weekly_dividend_twd = (
            estimated_annual_dividend_twd / 52.0
            if estimated_annual_dividend_twd is not None
            else None
        )
        return {
            "estimated_annual_dividend_twd": estimated_annual_dividend_twd,
            "estimated_weekly_dividend_twd": estimated_weekly_dividend_twd,
        }

    @staticmethod
    def _manual_validated_principal(
        symbol: str,
        market: str,
        currency: str,
        quantity: float,
        average_cost: float,
        optional: dict[str, Any],
    ) -> tuple[float | None, float | None]:
        if not symbol:
            raise ValueError("持股代號不可空白")
        if quantity < 0:
            raise ValueError("持股數量不可小於零")
        if average_cost < 0:
            raise ValueError("平均成本不可小於零")
        principal_amount = optional["principal_amount"]
        principal_twd = optional["principal_twd"]
        principal_currency = optional["principal_currency"]
        if principal_amount is None and average_cost > 0 and quantity > 0:
            principal_amount = average_cost * quantity
        if principal_twd is None and principal_currency == "TWD":
            principal_twd = principal_amount
        if not market:
            raise ValueError("請選擇市場")
        if not currency or len(currency) > 8 or not currency.replace("-", "").isalnum():
            raise ValueError("幣別格式不正確")
        if (
            not principal_currency
            or len(principal_currency) > 8
            or not principal_currency.replace("-", "").isalnum()
        ):
            raise ValueError("本金幣別格式不正確")
        return principal_amount, principal_twd

    @staticmethod
    def _manual_holding_dict(
        payload: dict[str, Any],
        existing: dict[str, Any] | None,
        parts: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **(existing or {}),
            "holding_id": str((existing or {}).get("holding_id") or payload.get("holding_id") or ""),
            "symbol": parts["symbol"],
            "name": str(payload.get("name") or parts["symbol"]).strip(),
            "market": parts["market"],
            "asset_type": parts["asset_type"],
            "quantity": parts["quantity"],
            "average_cost": parts["average_cost"],
            "currency": parts["currency"],
            "principal_amount": parts["principal_amount"],
            "principal_currency": parts["principal_currency"],
            "principal_twd": parts["principal_twd"],
            "fund_code": parts["fund_code"],
            "fund_isin": parts["fund_isin"],
            "fund_share_class": parts["fund_share_class"],
            "fund_quote_symbol": parts["fund_quote_symbol"],
            "fund_identity_status": (
                "confirmed"
                if parts["fund_quote_symbol"]
                else str((existing or {}).get("fund_identity_status") or "")
            ),
            "source_row": (existing or {}).get("source_row"),
            "dividend_amount_twd": parts["dividend_amount_twd"],
            "dividend_per_unit": parts["dividend_per_unit"],
            "monthly_dividend_twd": parts["monthly_dividend_twd"],
            "annual_dividend_yield_percent": parts["annual_dividend_yield_percent"],
            "dividend_frequency": parts["dividend_frequency"],
            "dividend_frequency_label": parts["dividend_frequency_label"],
            "dividend_frequency_per_year": parts["dividend_frequency_per_year"],
            "dividend_frequency_source": "manual",
            "dividend_frequency_confidence": 1.0,
            "payback_rate_percent": parts["payback_rate_percent"],
            "current_value_twd": parts["current_value_twd"],
            "estimated_annual_dividend_twd": parts["estimated_annual_dividend_twd"],
            "estimated_weekly_dividend_twd": parts["estimated_weekly_dividend_twd"],
            "manually_edited": True,
        }
