"""Analytics and ledger literal blocks for the synthetic smoke fixture."""

from __future__ import annotations

from typing import Any


def _performance_analytics(generated_at: str) -> dict[str, Any]:
    return {
        "status": "ready",
        "current_value": 141394,
        "current_cost": 131784,
        "unrealized_pnl": 9610,
        "unrealized_pnl_percent": 7.29,
        "realized_pnl": 3200,
        "dividend_income": 3460,
        "fees_and_taxes": 420,
        "twr_percent": 8.31,
        "xirr_percent": 7.82,
        "annualized_volatility_percent": 12.4,
        "max_drawdown_percent": -8.7,
        "snapshot_count": 12,
        "equity_curve": [
            {"date": "2026-04-01", "value": 128000},
            {"date": "2026-05-01", "value": 132500},
            {"date": "2026-06-01", "value": 137200},
            {"date": "2026-07-01", "value": 141394},
        ],
        "positions": [
            {"symbol": "VTI", "market_value": 70656, "weight_percent": 49.97},
            {"symbol": "BOND-DEMO", "market_value": 51250, "weight_percent": 36.25},
            {"symbol": "0050", "market_value": 19488, "weight_percent": 13.78},
        ],
        "attribution_by_currency": {
            "TWD": {"pnl": 2914},
            "USD": {"pnl": 6696},
        },
    }


def _risk_analytics() -> dict[str, Any]:
    return {
        "status": "ready",
        "sample_count": 120,
        "annualized_volatility_percent": 12.4,
        "beta": 0.73,
        "var_95_one_day_percent": -1.38,
        "cvar_95_one_day_percent": -2.05,
        "max_drawdown_percent": -8.7,
        "benchmark": "SPY",
        "correlations": [
            {"left": "0050", "right": "VTI", "correlation": 0.54},
            {"left": "VTI", "right": "BOND-DEMO", "correlation": -0.12},
        ],
        "risk_contributions": [
            {"symbol": "VTI", "risk_contribution_percent": 62.1},
            {"symbol": "0050", "risk_contribution_percent": 25.3},
            {"symbol": "BOND-DEMO", "risk_contribution_percent": 12.6},
        ],
        "exposures": {
            "market": {"US": 49.97, "FUND": 36.25, "TW": 13.78},
            "asset_type": {"ETF": 63.75, "BOND": 36.25},
            "currency": {"TWD": 50.03, "USD": 49.97},
        },
    }


def _ledger_transactions() -> list[dict[str, Any]]:
    return [
        {
            "transaction_id": "fixture-tx-1",
            "side": "BUY",
            "symbol": "0050",
            "quantity": 120,
            "price": 148.2,
            "currency": "TWD",
            "occurred_at": "2025-01-02T10:00:00+08:00",
            "status": "confirmed",
        },
        {
            "transaction_id": "fixture-tx-2",
            "side": "DIVIDEND",
            "symbol": "VTI",
            "amount": 860,
            "currency": "TWD",
            "occurred_at": "2026-06-30T00:00:00+08:00",
            "status": "confirmed",
        },
    ]


def _ledger_analytics() -> dict[str, Any]:
    return {
        "ledger_quality": "confirmed",
        "transaction_count": 5,
        "confirmed_transaction_count": 5,
        "estimated_transaction_count": 0,
        "realized_pnl": 3200,
        "dividend_income": 3460,
        "fees_and_taxes": 420,
        "opening_ledger": {
            "occurred_at": "2025-01-02T00:00:00+08:00",
            "generated_count": 3,
            "active_holding_count": 3,
            "uncovered_count": 0,
            "uncovered_symbols": [],
            "coverage_percent": 100,
        },
        "reconciliation": {
            "status": "matched",
            "symbol_count": 3,
            "matched_count": 3,
            "difference_count": 0,
            "coverage_percent": 100,
            "differences": [],
        },
        "transactions": _ledger_transactions(),
    }


def _alert_feed(generated_at: str) -> dict[str, Any]:
    return {
        "rules": [
            {
                "rule_id": "fixture-rule-1",
                "name": "單一部位上限",
                "rule_type": "concentration",
                "severity": "warning",
                "enabled": True,
            }
        ],
        "events": [
            {
                "alert_id": "fixture-alert-1",
                "title": "曝險接近政策上限",
                "severity": "warning",
                "created_at": generated_at,
                "acknowledged": False,
            }
        ],
        "new_count": 1,
        "unacknowledged_count": 1,
    }


def _calibration_analytics() -> dict[str, Any]:
    return {
        "status": "ready",
        "evaluated_count": 32,
        "pending_count": 4,
        "brier_score": 0.18,
        "calibration_label": "穩定",
        "accuracy_percent": 71.9,
        "average_confidence_percent": 69.4,
        "reliability_gap_percent": 2.5,
        "confidence_multiplier": 1.0,
        "confidence_buckets": [
            {"label": "50-70%", "count": 18, "accuracy_percent": 66.7},
            {"label": "70-90%", "count": 14, "accuracy_percent": 78.6},
        ],
    }


def _advisory_analytics(generated_at: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "event_id": "fixture-event-1",
                "event_type": "earnings",
                "symbol": "VTI",
                "title": "範例事件",
                "scheduled_at": "2026-08-05T20:00:00+08:00",
                "status": "upcoming",
            }
        ],
        "alerts": _alert_feed(generated_at),
        "decisions": [
            {
                "decision_id": "fixture-decision-1",
                "symbol": "VTI",
                "action": "維持觀察並確認政策限制",
                "confidence": 0.72,
                "user_status": "pending",
                "created_at": generated_at,
            }
        ],
        "calibration": _calibration_analytics(),
    }


def _analytics(generated_at: str) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "generated_at": generated_at,
        "data_health": {
            "schema_version": 1,
            "counts": {
                "prices": 360,
                "transactions": 5,
                "portfolio_snapshots": 12,
                "events": 2,
            },
            "history_ready": True,
            "ledger_ready": True,
            "ledger_quality": "confirmed",
            "warnings": [],
        },
        "privacy": {
            "state_encryption": "Windows DPAPI",
            "sensitive_field_encryption": "enabled",
            "platform_protected": True,
        },
        "performance": _performance_analytics(generated_at),
        "risk": _risk_analytics(),
        "stress": {
            "status": "ready",
            "current_value": 141394,
            "scenarios": [
                {"name": "全球股市 -20%", "estimated_change_percent": -12.8},
                {"name": "美元對台幣 -8%", "estimated_change_percent": -4.0},
            ],
        },
        "ledger": _ledger_analytics(),
        **_advisory_analytics(generated_at),
    }
