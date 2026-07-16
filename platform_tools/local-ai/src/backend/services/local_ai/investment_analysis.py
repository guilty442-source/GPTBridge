from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def analyze_investments(payload: dict[str, Any]) -> dict[str, Any]:
    holdings = [dict(item) for item in payload.get("holdings", []) if isinstance(item, dict)]
    parameters = dict(payload.get("analysis_parameters") or {})
    concentration_limit = _number(parameters.get("position_concentration_percent"), 20.0)
    missing_limit = _number(parameters.get("missing_data_warning_percent"), 5.0)
    active = [item for item in holdings if _number(item.get("quantity"), 0) > 0]
    total = sum(_number(item.get("current_value_twd"), _number(item.get("web_current_value_twd"), 0)) for item in active)
    missing = [item for item in active if not item.get("market_data_updated_at") and not item.get("observed_at")]
    currency_exposure: dict[str, float] = defaultdict(float)
    positions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for item in active:
        value = _number(item.get("current_value_twd"), _number(item.get("web_current_value_twd"), 0))
        weight = value / total * 100 if total > 0 else 0.0
        currency = str(item.get("currency") or "UNKNOWN").upper()
        currency_exposure[currency] += value
        position = {
            "symbol": str(item.get("symbol") or ""),
            "name": str(item.get("name") or ""),
            "asset_type": str(item.get("asset_type") or "AUTO").upper(),
            "currency": currency,
            "current_value_twd": round(value, 4),
            "portfolio_weight_percent": round(weight, 4),
            "quote_source": str(item.get("market_data_source") or ""),
            "quote_observed_at": str(item.get("market_data_updated_at") or item.get("observed_at") or ""),
        }
        positions.append(position)
        if weight > concentration_limit:
            warnings.append({"code": "POSITION_CONCENTRATION", "severity": "warning", "symbol": position["symbol"], "message": f"單一持倉占比 {weight:.2f}% 超過 {concentration_limit:.2f}% 分析門檻。"})
    missing_percent = len(missing) / len(active) * 100 if active else 0.0
    if missing_percent > missing_limit:
        warnings.append({"code": "MARKET_DATA_COVERAGE", "severity": "warning", "message": f"{missing_percent:.2f}% 有效持倉缺少具時間戳的市場資料。"})
    currency_weights = {
        currency: round(value / total * 100, 4) if total > 0 else 0.0
        for currency, value in sorted(currency_exposure.items())
    }
    positions.sort(key=lambda item: item["current_value_twd"], reverse=True)
    return {
        "ok": True,
        "owner": "星澄",
        "analysis_version": "1.0",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "queued": False,
        "portfolio": {
            "active_holding_count": len(active),
            "total_current_value_twd": round(total, 4),
            "market_data_coverage_percent": round(100 - missing_percent, 4),
            "currency_exposure_percent": currency_weights,
        },
        "positions": positions,
        "risk_warnings": warnings,
        "analysis_parameters": {
            "position_concentration_percent": concentration_limit,
            "missing_data_warning_percent": missing_limit,
        },
        "limitations": [
            "分析結果不是投資建議或交易指令。",
            "缺少來源、日期或幣別的數值不納入可信市場資料。",
        ],
    }
