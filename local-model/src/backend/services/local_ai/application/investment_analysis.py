from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


ANALYSIS_MODEL_KEYS = (
    "valuation",
    "income-distribution",
    "risk-volatility",
    "portfolio-concentration",
    "asset-allocation",
    "scenario-stress",
    "fee-efficiency",
    "return-trend",
)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _parameter(item: dict[str, Any], key: str, *aliases: str) -> Any:
    parameters = item.get("market_parameters")
    if not isinstance(parameters, dict):
        parameters = item.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    for candidate in (key, *aliases):
        value = item.get(candidate)
        if value not in (None, ""):
            return value
        value = parameters.get(candidate)
        if value not in (None, ""):
            return value
    return None


def _label(item: dict[str, Any], key: str, fallback: str = "UNKNOWN") -> str:
    value = str(_parameter(item, key) or fallback).strip()
    return value.upper() if value else fallback


def _value(item: dict[str, Any]) -> float:
    direct = _optional_number(item.get("current_value_twd"), item.get("web_current_value_twd"))
    if direct is not None:
        return max(0.0, direct)
    quantity = max(0.0, _number(item.get("quantity")))
    price = _optional_number(
        _parameter(item, "price", "web_current_price", "current_price", "nav")
    )
    return quantity * max(0.0, price or 0.0)


def _weighted_average(rows: list[tuple[float, float]]) -> float | None:
    denominator = sum(weight for _value, weight in rows if weight > 0)
    if denominator <= 0:
        return None
    return round(
        sum(value * weight for value, weight in rows if weight > 0) / denominator,
        6,
    )


def _exposure(active: list[dict[str, Any]], key: str, total: float) -> dict[str, float]:
    values: dict[str, float] = defaultdict(float)
    for item in active:
        values[_label(item, key)] += _value(item)
    return {
        label: round(value / total * 100, 4) if total > 0 else 0.0
        for label, value in sorted(values.items(), key=lambda entry: entry[1], reverse=True)
    }


def _model_result(
    model_key: str,
    *,
    coverage_count: int,
    population_count: int,
    metrics: dict[str, Any],
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    coverage = coverage_count / population_count * 100 if population_count else 0.0
    return {
        "model_key": model_key,
        "version": "1.0",
        "status": "completed" if coverage_count else "insufficient-data",
        "coverage_count": coverage_count,
        "coverage_percent": round(coverage, 4),
        "metrics": metrics,
        "evidence": list(evidence or [])[:100],
        "facts_locked": True,
    }


def analyze_investments(payload: dict[str, Any]) -> dict[str, Any]:
    holdings = [dict(item) for item in payload.get("holdings", []) if isinstance(item, dict)]
    parameters = dict(payload.get("analysis_parameters") or {})
    concentration_limit = _number(
        parameters.get("position_concentration_percent", parameters.get("max_single_position_percent")),
        20.0,
    )
    missing_limit = _number(parameters.get("missing_data_warning_percent"), 5.0)
    region_limit = _number(parameters.get("region_concentration_percent"), 55.0)
    industry_limit = _number(parameters.get("industry_concentration_percent"), 45.0)
    currency_limit = _number(parameters.get("currency_concentration_percent"), 65.0)
    volatility_limit = _number(parameters.get("volatility_warning_percent"), 25.0)
    annual_fee_limit = _number(parameters.get("annual_fee_warning_percent"), 1.5)
    valuation_limit = _number(parameters.get("valuation_move_warning_percent"), 15.0)
    stress_shock = abs(_number(parameters.get("stress_shock_percent"), 20.0))

    active = [item for item in holdings if _number(item.get("quantity"), 0) > 0]
    total = sum(_value(item) for item in active)
    missing = [
        item
        for item in active
        if not item.get("market_data_updated_at") and not item.get("observed_at")
    ]
    positions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    currency_exposure = _exposure(active, "currency", total)
    region_exposure = _exposure(active, "region", total)
    industry_exposure = _exposure(active, "industry", total)
    asset_exposure = _exposure(active, "asset_type", total)

    valuation_rows: list[tuple[float, float]] = []
    return_rows: list[tuple[float, float]] = []
    ytd_rows: list[tuple[float, float]] = []
    volatility_rows: list[tuple[float, float]] = []
    fee_rows: list[tuple[float, float]] = []
    income_rows: list[tuple[float, float]] = []
    stress_loss = 0.0

    for item in active:
        value = _value(item)
        weight = value / total * 100 if total > 0 else 0.0
        symbol = str(item.get("symbol") or "")
        price = _optional_number(_parameter(item, "price", "web_current_price", "current_price", "nav"))
        previous_close = _optional_number(_parameter(item, "previous_close"))
        change = _optional_number(_parameter(item, "change_percent"))
        if change is None and price is not None and previous_close not in (None, 0):
            change = (price - previous_close) / previous_close * 100
        principal = _optional_number(item.get("principal_twd"))
        cost_return = (
            (value - principal) / principal * 100
            if principal is not None and principal > 0
            else None
        )
        ytd = _optional_number(_parameter(item, "ytd_return_percent"))
        volatility = _optional_number(_parameter(item, "volatility_percent"))
        management_fee = _optional_number(_parameter(item, "management_fee_percent")) or 0.0
        custody_fee = _optional_number(_parameter(item, "custody_fee_percent")) or 0.0
        total_fee = max(0.0, management_fee + custody_fee)
        distribution_yield = _optional_number(_parameter(item, "distribution_yield_percent"))
        annual_distribution = _optional_number(_parameter(item, "annual_distribution_per_unit"))
        risk_level = str(_parameter(item, "risk_level") or "").upper()
        risk_multiplier = 1.0
        if risk_level:
            digits = [int(character) for character in risk_level if character.isdigit()]
            if digits:
                risk_multiplier = max(0.5, min(1.75, digits[0] / 3))
        if volatility is not None:
            risk_multiplier = max(risk_multiplier, min(2.0, max(0.5, volatility / 20)))
        position_stress_loss = value * min(1.0, stress_shock / 100 * risk_multiplier)
        stress_loss += position_stress_loss

        source = str(item.get("market_data_source") or item.get("quote_source") or "")
        source_url = str(item.get("market_data_source_url") or "")
        observed_at = str(item.get("market_data_updated_at") or item.get("observed_at") or "")
        source_confidence = _optional_number(_parameter(item, "source_confidence", "confidence"))
        if source or observed_at:
            evidence.append(
                {
                    "symbol": symbol,
                    "source": source,
                    "source_url": source_url,
                    "observed_at": observed_at,
                    "confidence": source_confidence,
                }
            )

        position = {
            "symbol": symbol,
            "name": str(item.get("name") or ""),
            "asset_type": _label(item, "asset_type", "AUTO"),
            "currency": _label(item, "currency"),
            "region": _label(item, "region"),
            "industry": _label(item, "industry"),
            "current_value_twd": round(value, 4),
            "portfolio_weight_percent": round(weight, 4),
            "price": price,
            "change_percent": round(change, 4) if change is not None else None,
            "cost_return_percent": round(cost_return, 4) if cost_return is not None else None,
            "ytd_return_percent": round(ytd, 4) if ytd is not None else None,
            "volatility_percent": round(volatility, 4) if volatility is not None else None,
            "annual_fee_percent": round(total_fee, 4),
            "distribution_yield_percent": distribution_yield,
            "stress_loss_twd": round(position_stress_loss, 4),
            "quote_source": source,
            "quote_observed_at": observed_at,
        }
        positions.append(position)

        if weight > concentration_limit:
            warnings.append(
                {
                    "code": "POSITION_CONCENTRATION",
                    "severity": "warning",
                    "symbol": symbol,
                    "message": f"單一持倉占比 {weight:.2f}% 超過 {concentration_limit:.2f}% 分析門檻。",
                }
            )
        if volatility is not None and volatility > volatility_limit:
            warnings.append(
                {
                    "code": "HIGH_VOLATILITY",
                    "severity": "warning",
                    "symbol": symbol,
                    "message": f"波動率 {volatility:.2f}% 超過 {volatility_limit:.2f}% 門檻。",
                }
            )
        if total_fee > annual_fee_limit:
            warnings.append(
                {
                    "code": "HIGH_ANNUAL_FEE",
                    "severity": "warning",
                    "symbol": symbol,
                    "message": f"年費率 {total_fee:.2f}% 超過 {annual_fee_limit:.2f}% 門檻。",
                }
            )
        if change is not None:
            valuation_rows.append((change, value))
            if abs(change) > valuation_limit:
                warnings.append(
                    {
                        "code": "VALUATION_MOVE",
                        "severity": "warning",
                        "symbol": symbol,
                        "message": f"可驗證價格變動 {change:.2f}% 超過 {valuation_limit:.2f}% 門檻。",
                    }
                )
        if cost_return is not None:
            return_rows.append((cost_return, value))
        if ytd is not None:
            ytd_rows.append((ytd, value))
        if volatility is not None:
            volatility_rows.append((volatility, value))
        if total_fee > 0:
            fee_rows.append((total_fee, value))
        if distribution_yield is not None:
            income_rows.append((distribution_yield, value))
        elif annual_distribution is not None and price not in (None, 0):
            income_rows.append((annual_distribution / price * 100, value))

    missing_percent = len(missing) / len(active) * 100 if active else 0.0
    if missing_percent > missing_limit:
        warnings.append(
            {
                "code": "MARKET_DATA_COVERAGE",
                "severity": "warning",
                "message": f"{missing_percent:.2f}% 有效持倉缺少具時間戳的市場資料。",
            }
        )
    for label, exposure, limit, code in (
        ("幣別", currency_exposure, currency_limit, "CURRENCY_CONCENTRATION"),
        ("區域", region_exposure, region_limit, "REGION_CONCENTRATION"),
        ("產業", industry_exposure, industry_limit, "INDUSTRY_CONCENTRATION"),
    ):
        for name, percentage in exposure.items():
            if name != "UNKNOWN" and percentage > limit:
                warnings.append(
                    {
                        "code": code,
                        "severity": "warning",
                        "dimension": name,
                        "message": f"{label} {name} 曝險 {percentage:.2f}% 超過 {limit:.2f}% 門檻。",
                    }
                )

    model_results = {
        "valuation": _model_result(
            "valuation",
            coverage_count=len(valuation_rows),
            population_count=len(active),
            metrics={"weighted_price_change_percent": _weighted_average(valuation_rows)},
            evidence=evidence,
        ),
        "income-distribution": _model_result(
            "income-distribution",
            coverage_count=len(income_rows),
            population_count=len(active),
            metrics={"weighted_distribution_yield_percent": _weighted_average(income_rows)},
            evidence=evidence,
        ),
        "risk-volatility": _model_result(
            "risk-volatility",
            coverage_count=len(volatility_rows),
            population_count=len(active),
            metrics={"weighted_volatility_percent": _weighted_average(volatility_rows)},
            evidence=evidence,
        ),
        "portfolio-concentration": _model_result(
            "portfolio-concentration",
            coverage_count=len(active),
            population_count=len(active),
            metrics={
                "largest_position_percent": max((item["portfolio_weight_percent"] for item in positions), default=0.0),
                "position_hhi": round(sum((item["portfolio_weight_percent"] / 100) ** 2 for item in positions), 6),
            },
        ),
        "asset-allocation": _model_result(
            "asset-allocation",
            coverage_count=len(active),
            population_count=len(active),
            metrics={
                "asset_exposure_percent": asset_exposure,
                "currency_exposure_percent": currency_exposure,
                "region_exposure_percent": region_exposure,
                "industry_exposure_percent": industry_exposure,
            },
        ),
        "scenario-stress": _model_result(
            "scenario-stress",
            coverage_count=len(active),
            population_count=len(active),
            metrics={
                "shock_percent": -stress_shock,
                "estimated_loss_twd": round(stress_loss, 4),
                "estimated_remaining_value_twd": round(max(0.0, total - stress_loss), 4),
                "estimated_loss_percent": round(stress_loss / total * 100, 4) if total else 0.0,
            },
        ),
        "fee-efficiency": _model_result(
            "fee-efficiency",
            coverage_count=len(fee_rows),
            population_count=len(active),
            metrics={
                "weighted_annual_fee_percent": _weighted_average(fee_rows),
                "estimated_annual_fee_twd": round(sum(value * fee / 100 for fee, value in fee_rows), 4),
            },
            evidence=evidence,
        ),
        "return-trend": _model_result(
            "return-trend",
            coverage_count=max(len(return_rows), len(ytd_rows), len(valuation_rows)),
            population_count=len(active),
            metrics={
                "weighted_cost_return_percent": _weighted_average(return_rows),
                "weighted_ytd_return_percent": _weighted_average(ytd_rows),
                "weighted_short_term_change_percent": _weighted_average(valuation_rows),
            },
            evidence=evidence,
        ),
    }

    positions.sort(key=lambda item: item["current_value_twd"], reverse=True)
    completed_count = sum(
        result["status"] == "completed" for result in model_results.values()
    )
    return {
        "ok": True,
        "owner": "星澄",
        "analysis_version": "1.0",
        "analysis_schema": "star-investment-analysis/v1",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "queued": False,
        "portfolio": {
            "active_holding_count": len(active),
            "total_current_value_twd": round(total, 4),
            "market_data_coverage_percent": round(100 - missing_percent, 4),
            "currency_exposure_percent": currency_exposure,
            "region_exposure_percent": region_exposure,
            "industry_exposure_percent": industry_exposure,
            "asset_exposure_percent": asset_exposure,
        },
        "positions": positions,
        "risk_warnings": warnings,
        "model_results": model_results,
        "model_execution": {
            "registered_models": list(ANALYSIS_MODEL_KEYS),
            "executed_model_count": len(model_results),
            "completed_with_data_count": completed_count,
            "coverage_percent": round(completed_count / len(model_results) * 100, 4),
        },
        "evidence": evidence,
        "analysis_parameters": {
            "position_concentration_percent": concentration_limit,
            "missing_data_warning_percent": missing_limit,
            "region_concentration_percent": region_limit,
            "industry_concentration_percent": industry_limit,
            "currency_concentration_percent": currency_limit,
            "volatility_warning_percent": volatility_limit,
            "annual_fee_warning_percent": annual_fee_limit,
            "valuation_move_warning_percent": valuation_limit,
            "stress_shock_percent": stress_shock,
        },
        "limitations": [
            "分析結果不是投資建議或交易指令。",
            "缺少來源、日期或幣別的數值不納入可信市場資料。",
            "壓力測試是可重現情境估算，不代表未來損失預測。",
        ],
    }
