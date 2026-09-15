"""Literal data builders for the synthetic visual-smoke fixture."""

from __future__ import annotations

from typing import Any

from smoke_fixture_analytics import _analytics


def _holding_tw(generated_at: str) -> dict[str, Any]:
    return {
        "holding_id": "fixture-tw",
        "symbol": "0050",
        "name": "台灣大型股 ETF",
        "market": "TW",
        "asset_type": "ETF",
        "quantity": 120,
        "average_cost": 148.2,
        "currency": "TWD",
        "principal_amount": 17784,
        "principal_currency": "TWD",
        "principal_twd": 17784,
        "web_current_price": 162.4,
        "web_current_price_currency": "TWD",
        "web_current_value_twd": 19488,
        "current_value_twd": 19488,
        "market_data_source": "synthetic_fixture",
        "market_data_updated_at": generated_at,
        "estimated_annual_dividend_twd": 480,
        "estimated_weekly_dividend_twd": 9.23,
        "dividend_frequency_label": "每季",
    }


def _holding_us(generated_at: str) -> dict[str, Any]:
    return {
        "holding_id": "fixture-us",
        "symbol": "VTI",
        "name": "美國全市場 ETF",
        "market": "US",
        "asset_type": "ETF",
        "quantity": 8,
        "average_cost": 250,
        "currency": "USD",
        "principal_amount": 2000,
        "principal_currency": "USD",
        "principal_twd": 64000,
        "web_current_price": 276,
        "web_current_price_currency": "USD",
        "web_current_value_twd": 70656,
        "current_value_twd": 70656,
        "market_data_source": "synthetic_fixture",
        "market_data_updated_at": generated_at,
        "estimated_annual_dividend_twd": 1180,
        "estimated_weekly_dividend_twd": 22.69,
        "dividend_frequency_label": "每季",
    }


def _holding_bond(generated_at: str) -> dict[str, Any]:
    return {
        "holding_id": "fixture-bond",
        "symbol": "BOND-DEMO",
        "name": "投資級債券範例",
        "market": "FUND",
        "asset_type": "BOND",
        "quantity": 50,
        "average_cost": 1000,
        "currency": "TWD",
        "principal_amount": 50000,
        "principal_currency": "TWD",
        "principal_twd": 50000,
        "current_value_twd": 51250,
        "market_data_source": "synthetic_fixture",
        "market_data_updated_at": generated_at,
        "estimated_annual_dividend_twd": 1800,
        "estimated_weekly_dividend_twd": 34.62,
        "dividend_frequency_label": "每月",
    }


def _xingcheng_product_status(generated_at: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "state_label": "本地風險訊號中等",
        "score": 42,
        "risk_level": "medium",
        "risk_level_label": "中等",
        "decision_summary": "風險代理只描述曝險，不代表看多或買進建議。",
        "confidence_label": "證據覆蓋良好",
        "confidence_score": 88,
        "network_enabled": False,
        "network_mode": "offline",
        "network_mode_label": "離線",
        "quote_health": "healthy",
        "quote_health_label": "合成報價完整",
        "coverage_label": "100%",
        "warning_count": 1,
        "critical_count": 0,
        "offline_mode": True,
        "has_portfolio": True,
        "portfolio_count": 3,
        "next_actions": ["確認單一市場曝險", "檢查現金需求"],
        "command_suggestions": ["分析組合集中度", "列出反證條件"],
        "generated_at": generated_at,
    }


def _xingcheng_misc() -> dict[str, Any]:
    return {
        "xingcheng_risk_warnings": [
            {
                "severity": "warning",
                "code": "fixture_concentration",
                "title": "美國市場曝險需人工確認",
                "detail": "這是合成 smoke 資料，不是投資建議。",
                "action": "確認風險承受力與時間期限。",
                "symbol": "VTI",
            }
        ],
        "xingcheng_decision_brief": "證據完整時才提供風險說明；所有動作仍需人工核准。",
        "xingcheng_external_discussion": {
            "ok": True,
            "queued": False,
            "provider": "chatgpt",
            "status": "completed",
            "content": "ChatGPT 已完成最終統籌，結果經 ai-collaboration 回傳投資管家顯示。",
            "response_recipient": "ai-assistant",
            "transport": "governance-authenticated-ai-channel",
        },
        "xingcheng_confidence": {
            "score": 88,
            "label": "證據覆蓋良好",
            "sample_count": 3,
            "low_confidence_symbols": [],
        },
        "xingcheng_explanation": {
            "mode": "deterministic",
            "mode_label": "規則引擎說明",
            "model": "",
            "text": "合成資料顯示市場曝險分散，但資料日期與政策仍需人工確認。",
            "facts_locked": True,
        },
    }


def _workbook(generated_at: str) -> dict[str, Any]:
    return {
        "workbook_scan": {
            "sheet_count": 3,
            "selected_sheet": {
                "sheet_name": "持股總表",
                "header_row_number": 2,
                "data_start_row_number": 3,
                "header_mode": "two_row_header",
                "valid_data_row_count": 3,
            },
        },
        "workbook_scan_quality": {
            "state": "ready",
            "state_label": "可用",
            "score": 96,
            "selected_sheet_name": "持股總表",
            "header_row_number": 2,
            "header_mode": "two_row_header",
            "header_depth": 2,
            "valid_data_row_count": 3,
            "recommendation": "合成資料僅用於介面與無障礙驗證。",
        },
    }


def _market_sessions(generated_at: str) -> dict[str, Any]:
    return {
        "as_of": generated_at,
        "open_markets": [],
        "markets": {
            "TW": {
                "is_open": False,
                "timezone": "Asia/Taipei",
                "schedule": "09:00-13:30",
                "local_time": "23:30",
            }
        },
    }


def _v3_policy(generated_at: str) -> dict[str, Any]:
    return {
        "investment_policy": {
            "investment_goal": "退休資產穩健增值",
            "time_horizon_years": 10,
            "risk_capacity": "balanced",
            "cash_need_percent": 8,
            "forbidden_assets": ["CRYPTO"],
            "target_return_percent": 6,
            "human_approval_required": True,
            "automatic_order_submission": False,
        },
        "multi_currency": {
            "status": "ready",
            "base_currency": "TWD",
            "market_value_base": 141394,
            "total_pnl_base": 9610,
            "asset_pnl_base": 7200,
            "currency_pnl_base": 2410,
            "missing_currencies": [],
            "positions": [
                {"symbol": "0050", "currency": "TWD", "market_value_base": 19488},
                {"symbol": "VTI", "currency": "USD", "market_value_base": 70656},
                {"symbol": "BOND-DEMO", "currency": "TWD", "market_value_base": 51250},
            ],
        },
    }


def _v3_analysis(generated_at: str) -> dict[str, Any]:
    return {
        "regime": {
            "status": "ready",
            "regime": "sideways",
            "label": "盤整",
            "confidence": 0.68,
        },
        "factors": {
            "factors": [
                {"factor": "market", "label": "市場", "exposure": 0.72},
                {"factor": "quality", "label": "品質", "exposure": 0.31},
            ],
            "symbol_contributions": [
                {"symbol": "VTI", "contribution_percent": 5.1},
                {"symbol": "0050", "contribution_percent": 2.2},
            ],
        },
        "corporate_actions": {
            "actions": [
                {
                    "action_id": "fixture-action-1",
                    "symbol": "VTI",
                    "action_type": "dividend",
                    "status": "approved",
                    "effective_date": "2026-06-30",
                }
            ],
            "issues": [],
        },
        "broker_imports": [],
    }


def _v3_automation_notifications(generated_at: str) -> dict[str, Any]:
    return {
        "automation": {
            "running": False,
            "interval_seconds": 900,
            "last_run": {
                "status": "completed",
                "started_at": generated_at,
                "finished_at": generated_at,
            },
            "next_run_at": "2026-07-28T15:45:00+00:00",
        },
        "notifications": {
            "channels": [
                {"channel_id": "windows_local", "enabled": True, "label": "Windows"},
                {"channel_id": "email_smtp", "enabled": False, "label": "Email"},
            ],
            "outbox": [
                {
                    "notification_id": "fixture-notice-1",
                    "channel_id": "windows_local",
                    "title": "風險摘要已送達",
                    "status": "delivered",
                    "created_at": generated_at,
                }
            ],
        },
    }


def _v3_governance_security(generated_at: str) -> dict[str, Any]:
    return {
        "model_governance": {
            "status": "ready",
            "version_count": 1,
            "run_count": 32,
            "evaluated_run_count": 32,
            "average_brier_score": 0.18,
        },
        "database_security": {
            "encrypted_at_rest": True,
            "provider": "Windows DPAPI",
            "key_id": "fixture-key-2026",
            "integrity_verified": True,
            "last_verified_at": generated_at,
        },
        "backups": [
            {
                "name": "fixture-backup.ivault",
                "created_at": generated_at,
                "status": "verified",
                "size_bytes": 204800,
            }
        ],
        "audit_log": [
            {
                "audit_id": "fixture-audit-1",
                "action": "visual_smoke",
                "severity": "info",
                "occurred_at": generated_at,
            }
        ],
    }


def _v3_state(generated_at: str) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        **_v3_policy(generated_at),
        **_v3_analysis(generated_at),
        **_v3_automation_notifications(generated_at),
        **_v3_governance_security(generated_at),
    }


def fixture_state(generated_at: str) -> dict[str, Any]:
    return {
        "updated_at": generated_at,
        "portfolio": {
            "file_name": "範例投資組合.xlsx",
            "holding_count": 3,
            "imported_at": generated_at,
            "source_created_at": "2025-01-02T00:00:00+08:00",
            "manual_revision": 2,
        },
        "holdings": [
            _holding_tw(generated_at),
            _holding_us(generated_at),
            _holding_bond(generated_at),
        ],
        "ai_runs": [
            {
                "run_id": "fixture-run-1",
                "role": "risk_review",
                "provider": "local_rules",
                "status": "completed",
                "content": "僅供視覺 smoke 的合成結果。",
                "error": "",
                "created_at": generated_at,
            }
        ],
        **_workbook(generated_at),
        "xingcheng_product_status": _xingcheng_product_status(generated_at),
        **_xingcheng_misc(),
        "portfolio_versions": [
            {
                "version_id": "fixture-v2",
                "created_at": generated_at,
                "reason": "visual-smoke",
                "holding_count": 3,
                "file_name": "範例投資組合.xlsx",
                "manual_revision": 2,
            }
        ],
        "market_sessions": _market_sessions(generated_at),
        "analytics": _analytics(generated_at),
        "v3": _v3_state(generated_at),
    }


def _diagnostics_xingcheng(generated_at: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "state_label": "本地風險訊號中等",
        "score": 42,
        "risk_level": "medium",
        "risk_level_label": "中等",
        "network_enabled": False,
        "network_mode": "offline",
        "network_mode_label": "離線",
        "quote_health": "healthy",
        "quote_health_label": "合成報價完整",
        "coverage_percent": 100,
        "coverage_label": "100%",
        "warning_count": 1,
        "critical_count": 0,
        "offline_mode": True,
        "generated_at": generated_at,
    }


def _diagnostics_portfolio_workbook(generated_at: str) -> dict[str, Any]:
    return {
        "portfolio": {
            "file_name": "範例投資組合.xlsx",
            "holding_count": 3,
            "imported_at": generated_at,
            "age_hours": 0.1,
            "stale": False,
        },
        "workbook": {
            "state": "ready",
            "state_label": "可用",
            "score": 96,
            "sheet_count": 3,
            "selected_sheet_name": "持股總表",
            "header_row_number": 2,
            "header_depth": 2,
            "valid_data_row_count": 3,
            "recommendation": "合成資料僅用於 smoke。",
        },
    }


def fixture_diagnostics(generated_at: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "state_label": "可用",
        "message": "合成資料已載入；目前只進行本地視覺與無障礙檢查。",
        "generated_at": generated_at,
        "data_quality": {
            "state": "ready",
            "holding_count": 3,
            "invalid_symbol_count": 0,
            "decimal_symbol_count": 0,
            "invalid_symbol_percent": 0,
            "risk_analysis_suspended": False,
        },
        "error_logging": {
            "enabled": True,
            "count": 0,
            "format": "redacted_jsonl",
        },
        **_diagnostics_portfolio_workbook(generated_at),
        "xingcheng": _diagnostics_xingcheng(generated_at),
        "runs": {
            "count": 1,
            "latest": {
                "run_id": "fixture-run-1",
                "role": "risk_review",
                "provider": "local_rules",
                "status": "completed",
                "created_at": generated_at,
            },
        },
        "boundaries": {
            "local_only": True,
            "external_ai": False,
            "service_commands": [],
        },
    }


def fixture_mobile_sync() -> dict[str, Any]:
    return {
        "enabled": False,
        "running": False,
        "mode": "loopback",
        "mode_label": "僅限本機",
        "remote_ready": False,
        "remote_status_label": "不同網路未設定",
        "relay_required": True,
        "port": 18765,
        "bind_host": "127.0.0.1",
        "pairing_code": "",
        "loopback_url": "",
        "local_urls": [],
        "remote_base_url": "",
        "remote_url": "",
        "access_scope": "read_state_and_queue_xingcheng_command",
        "session_count": 0,
        "session_idle_minutes": 30,
        "pairing_active": False,
        "pairing_expired": False,
        "pairing_revoked": False,
        "pairing_ttl_hours": 1,
        "rate_limit": "120/60s per address",
    }
