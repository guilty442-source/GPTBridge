from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Iterator

try:
    from playwright.sync_api import Browser, Page, sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[assignment]
    Browser = None  # type: ignore[assignment]
    Page = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RENDERER_DIR = (
    PROJECT_ROOT
    / "dist-ui"
    / "independent-tools"
    / "ai-assistant"
    / "renderer"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "test-results" / "ai-assistant-visual"
WORKSPACE_LABELS = ["持股", "星澄", "星澄帳務", "系統"]


def synthetic_fixture() -> dict[str, Any]:
    generated_at = "2026-07-28T15:30:00+00:00"
    state = {
        "updated_at": generated_at,
        "portfolio": {
            "file_name": "範例投資組合.xlsx",
            "holding_count": 3,
            "imported_at": generated_at,
            "source_created_at": "2025-01-02T00:00:00+08:00",
            "manual_revision": 2,
        },
        "holdings": [
            {
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
            },
            {
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
            },
            {
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
            },
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
        "xingcheng_product_status": {
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
        },
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
            "content": "ChatGPT 已完成最終統籌，結果先回傳星澄再提供投資管家顯示。",
            "response_recipient": "xingcheng",
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
        "market_sessions": {
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
        },
        "analytics": {
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
            "performance": {
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
            },
            "risk": {
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
            },
            "stress": {
                "status": "ready",
                "current_value": 141394,
                "scenarios": [
                    {"name": "全球股市 -20%", "estimated_change_percent": -12.8},
                    {"name": "美元對台幣 -8%", "estimated_change_percent": -4.0},
                ],
            },
            "ledger": {
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
                "transactions": [
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
                ],
            },
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
            "alerts": {
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
            },
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
            "calibration": {
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
            },
        },
        "v3": {
            "version": "1.0.0",
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
        },
    }
    return {
        "state_revision": "visual-fixture-v1",
        "state": state,
        "diagnostics": {
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
            "xingcheng": {
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
            },
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
        },
        "mobile_sync": {
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
        },
        "market_sessions": state["market_sessions"],
    }


def browser_bootstrap_script(fixture: dict[str, Any]) -> str:
    encoded = json.dumps(fixture, ensure_ascii=False, separators=(",", ":"))
    return f"""
(() => {{
  const fixture = {encoded};
  const revision = fixture.state_revision;
  class VisualSmokeWebSocket {{
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url) {{
      this.url = url;
      this.readyState = VisualSmokeWebSocket.CONNECTING;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      window.setTimeout(() => {{
        this.readyState = VisualSmokeWebSocket.OPEN;
        if (this.onopen) this.onopen({{ type: "open" }});
      }}, 0);
    }}
    send(raw) {{
      const request = JSON.parse(String(raw || "{{}}"));
      const command = String(request.command || "");
      const input = request.payload && typeof request.payload === "object"
        ? request.payload
        : {{}};
      const requestId = String(input.request_id || "");
      let payload = {{
        ok: true,
        message: "visual smoke mock",
        request_id: requestId,
        state_revision: revision,
      }};
      if (command === "investment_watch_get_state") {{
        const unchanged =
          String(input.state_revision || "") === revision && !Boolean(input.force);
        payload = unchanged
          ? {{
              ...payload,
              not_modified: true,
              mobile_sync: fixture.mobile_sync,
              market_sessions: fixture.market_sessions,
            }}
          : {{
              ...payload,
              state: fixture.state,
              diagnostics: fixture.diagnostics,
              mobile_sync: fixture.mobile_sync,
              market_sessions: fixture.market_sessions,
            }};
      }} else if (command === "investment_watch_sync_open_markets") {{
        payload = {{
          ...payload,
          mobile_sync: fixture.mobile_sync,
          market_sessions: fixture.market_sessions,
        }};
      }}
      window.setTimeout(() => {{
        if (!this.onmessage) return;
        this.onmessage({{
          data: JSON.stringify({{
            event: `${{command}}_result`,
            payload,
          }}),
        }});
      }}, 0);
    }}
    close() {{
      this.readyState = VisualSmokeWebSocket.CLOSED;
      if (this.onclose) this.onclose({{ type: "close" }});
    }}
  }}
  Object.defineProperty(window, "WebSocket", {{
    configurable: true,
    value: VisualSmokeWebSocket,
  }});
  Object.defineProperty(window, "electron", {{
    configurable: true,
    value: {{
      invoke: async (channel) => {{
        if (channel === "app:ensure-backend-started") {{
          return {{ ok: true, packageDigest: "visual-smoke" }};
        }}
        if (channel === "app:get-backend-session") {{
          return {{
            websocketUrl: "ws://127.0.0.1:8765/?visual-smoke=1",
            packageDigest: "visual-smoke",
            protocolVersion: 1,
            backendVersion: "1.0.0",
            startupError: "",
          }};
        }}
        return "";
      }},
    }},
  }});
  Object.defineProperty(window, "gptBridge", {{
    configurable: true,
    value: {{
      standaloneTool: false,
      openFile: async () => "",
    }},
  }});
  window.confirm = () => true;
}})();
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


@contextmanager
def renderer_server(directory: Path) -> Iterator[str]:
    handler = partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="AIInvestmentVisualSmokeHTTP",
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def build_renderer() -> None:
    command = [
        sys.executable,
        str(
            PROJECT_ROOT
            / "main-system"
            / "src"
            / "backend"
            / "services"
            / "main-system"
            / "integration"
            / "platform_packager.py"
        ),
        "ai-assistant",
        "--build-renderers-only",
        "--json",
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    if completed.returncode != 0 or not (RENDERER_DIR / "index.html").exists():
        raise RuntimeError(
            "AI assistant renderer build failed:\n"
            + completed.stdout[-6000:]
        )


def accessibility_audit(page: Page) -> list[dict[str, str]]:
    return page.evaluate(
        """
() => {
  const issues = [];
  const visible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      Number(style.opacity || 1) > 0 &&
      rect.width > 0 &&
      rect.height > 0
    );
  };
  const nodeName = (element) => {
    const id = element.id ? `#${element.id}` : "";
    const classes =
      typeof element.className === "string" && element.className.trim()
        ? "." + element.className.trim().split(/\\s+/).slice(0, 2).join(".")
        : "";
    return `${element.tagName.toLowerCase()}${id}${classes}`;
  };
  const labelText = (element) => {
    const aria = String(element.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    const labelledBy = String(element.getAttribute("aria-labelledby") || "")
      .trim()
      .split(/\\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id)?.textContent || "")
      .join(" ")
      .trim();
    if (labelledBy) return labelledBy;
    const id = String(element.id || "");
    const explicit = id
      ? document.querySelector(`label[for="${CSS.escape(id)}"]`)
      : null;
    if (explicit && String(explicit.textContent || "").trim()) {
      return String(explicit.textContent || "").trim();
    }
    const wrapping = element.closest("label");
    if (wrapping && String(wrapping.textContent || "").trim()) {
      return String(wrapping.textContent || "").trim();
    }
    return "";
  };

  if (document.documentElement.lang !== "zh-Hant") {
    issues.push({ code: "document_lang", target: "html", detail: "Expected lang=zh-Hant" });
  }
  if (document.querySelectorAll("main").length !== 1) {
    issues.push({ code: "main_landmark", target: "document", detail: "Expected exactly one main landmark" });
  }
  if (document.querySelectorAll("h1").length !== 1) {
    issues.push({ code: "primary_heading", target: "document", detail: "Expected exactly one h1" });
  }

  const ids = new Map();
  document.querySelectorAll("[id]").forEach((element) => {
    const value = element.id;
    ids.set(value, (ids.get(value) || 0) + 1);
  });
  ids.forEach((count, id) => {
    if (count > 1) {
      issues.push({ code: "duplicate_id", target: `#${id}`, detail: `ID appears ${count} times` });
    }
  });

  document.querySelectorAll("button, a[href]").forEach((element) => {
    if (!visible(element)) return;
    const name = String(
      element.getAttribute("aria-label") ||
      element.textContent ||
      element.getAttribute("title") ||
      ""
    ).trim();
    if (!name) {
      issues.push({ code: "control_name", target: nodeName(element), detail: "Visible control has no accessible name" });
    }
  });

  document.querySelectorAll("input, select, textarea").forEach((element) => {
    if (!visible(element) || element.getAttribute("type") === "hidden") return;
    if (!labelText(element)) {
      issues.push({ code: "form_label", target: nodeName(element), detail: "Visible form control has no label" });
    }
  });

  document.querySelectorAll("img").forEach((element) => {
    if (visible(element) && !element.hasAttribute("alt")) {
      issues.push({ code: "image_alt", target: nodeName(element), detail: "Visible image has no alt attribute" });
    }
  });

  document.querySelectorAll("table").forEach((element) => {
    if (!visible(element)) return;
    const caption = element.querySelector("caption");
    const labelled =
      String(element.getAttribute("aria-label") || "").trim() ||
      String(element.getAttribute("aria-labelledby") || "").trim();
    if (!caption && !labelled) {
      issues.push({ code: "table_caption", target: nodeName(element), detail: "Visible table has no caption or ARIA label" });
    }
  });

  const viewportOverflow =
    document.documentElement.scrollWidth - window.innerWidth;
  if (viewportOverflow > 2) {
    issues.push({
      code: "horizontal_overflow",
      target: "document",
      detail: `Document is ${Math.round(viewportOverflow)}px wider than the viewport`,
    });
  }
  return issues;
}
"""
    )


def wait_for_fixture(page: Page) -> None:
    page.get_by_role("heading", name="AI投資管家", exact=True).wait_for(
        state="visible",
        timeout=15_000,
    )
    page.wait_for_function(
        "() => document.querySelectorAll('.nexus-holding-row').length === 3",
        timeout=15_000,
    )


def exercise_holding_editor(page: Page) -> dict[str, Any]:
    edit_buttons = page.get_by_role("button", name="編輯", exact=True)
    if edit_buttons.count() < 1:
        raise AssertionError("Expected at least one holding edit button")
    edit_buttons.last.scroll_into_view_if_needed()
    scroll_before = page.evaluate("window.scrollY")
    edit_buttons.last.click()
    dialog = page.get_by_role("dialog", name="修改持股")
    dialog.wait_for(state="visible", timeout=5_000)
    box = dialog.bounding_box()
    if box is None:
        raise AssertionError("Holding editor dialog has no layout box")
    viewport_height = page.viewport_size["height"] if page.viewport_size else 0
    visible = box["y"] >= 0 and box["y"] + box["height"] <= viewport_height + 1
    page.get_by_role("button", name="取消", exact=True).click()
    dialog.wait_for(state="hidden", timeout=5_000)
    return {
        "opened_as_dialog": True,
        "fully_visible": visible,
        "scroll_preserved": page.evaluate("window.scrollY") == scroll_before,
    }


def exercise_workspaces(
    page: Page, output: Path, *, viewport_name: str
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for index, label in enumerate(WORKSPACE_LABELS, start=1):
        locator = page.get_by_role("button", name=label, exact=True)
        count = locator.count()
        if count != 1:
            raise AssertionError(f"Expected one {label} workspace button, found {count}")
        locator.click()
        page.wait_for_function(
            "(label) => document.querySelector('.nexus-workspace-nav button.is-active')?.textContent?.trim() === label",
            arg=label,
        )
        results[label] = {
            "selected": True,
            "content_visible": page.locator("main.nexus-app").is_visible(),
        }
        if label == "星澄":
            coordinator = page.get_by_text("ChatGPT 已統籌", exact=True)
            results[label]["chatgpt_coordinator_visible"] = coordinator.is_visible()
            if not coordinator.is_visible():
                raise AssertionError("ChatGPT coordination status is not visible")
        page.screenshot(
            path=str(output / f"{viewport_name}-workspace-{index}-{label}.png")
        )
    return results


def run_page(
    browser: Browser,
    url: str,
    fixture: dict[str, Any],
    output: Path,
    *,
    viewport: dict[str, int],
    mobile: bool,
    headed: bool,
) -> dict[str, Any]:
    context = browser.new_context(
        viewport=viewport,
        locale="zh-TW",
        color_scheme="dark",
        reduced_motion="reduce",
    )
    context.add_init_script(browser_bootstrap_script(fixture))
    page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    request_failures: list[str] = []
    page.on(
        "console",
        lambda message: (
            console_errors.append(message.text)
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: request_failures.append(
            f"{request.method} {request.url}: {request.failure}"
        ),
    )
    page.goto(url, wait_until="networkidle", timeout=30_000)
    wait_for_fixture(page)

    prefix = "mobile" if mobile else "desktop"
    page.screenshot(path=str(output / f"{prefix}-top.png"))
    holding_editor = exercise_holding_editor(page)
    workspace_results = exercise_workspaces(
        page, output, viewport_name=prefix
    )

    accessibility = accessibility_audit(page)
    result = {
        "viewport": viewport,
        "workspaces": workspace_results,
        "holding_editor": holding_editor,
        "accessibility_issues": accessibility,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "request_failures": request_failures,
        "document_size": page.evaluate(
            "() => ({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight, viewportWidth: innerWidth, viewportHeight: innerHeight })"
        ),
    }
    if headed:
        page.wait_for_timeout(1200)
    context.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build and visually smoke-test the AI investment manager renderer "
            "with synthetic local-only data."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Use the existing platform renderer output.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chromium windows briefly while the smoke runs.",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    if not args.skip_build:
        build_renderer()
    if not (RENDERER_DIR / "index.html").exists():
        raise FileNotFoundError(
            f"Renderer entry is missing: {RENDERER_DIR / 'index.html'}"
        )

    fixture = synthetic_fixture()
    report: dict[str, Any] = {
        "ok": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "renderer": str(RENDERER_DIR),
        "synthetic_data": True,
        "desktop": {},
        "mobile": {},
    }
    try:
        if sync_playwright is None:
            report["desktop"] = {"error": "playwright-not-installed"}
            report["mobile"] = {"error": "playwright-not-installed"}
        else:
            with renderer_server(RENDERER_DIR) as url, sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=not args.headed)
                try:
                    report["desktop"] = run_page(
                        browser,
                        url,
                        fixture,
                        output,
                        viewport={"width": 1440, "height": 920},
                        mobile=False,
                        headed=args.headed,
                    )
                    report["mobile"] = run_page(
                        browser,
                        url,
                        fixture,
                        output,
                        viewport={"width": 390, "height": 844},
                        mobile=True,
                        headed=args.headed,
                    )
                finally:
                    browser.close()
    except Exception as error:
        report["fatal_error"] = f"{type(error).__name__}: {error}"

    failures: list[str] = []
    for surface in ("desktop", "mobile"):
        result = report.get(surface) or {}
        for key in (
            "accessibility_issues",
            "console_errors",
            "page_errors",
            "request_failures",
        ):
            values = result.get(key) or []
            if values:
                failures.append(f"{surface}.{key}: {len(values)}")
        for label, workspace in (result.get("workspaces") or {}).items():
            if not workspace.get("selected") or not workspace.get("content_visible"):
                failures.append(f"{surface}.workspace.{label}")
    for surface in ("desktop", "mobile"):
        editor = (report.get(surface) or {}).get("holding_editor") or {}
        if editor and (
            not editor.get("opened_as_dialog")
            or not editor.get("fully_visible")
            or not editor.get("scroll_preserved")
        ):
            failures.append(f"{surface}.holding_editor: {editor}")
    if report.get("fatal_error"):
        failures.append(str(report["fatal_error"]))
    report["failures"] = failures
    report["ok"] = not failures

    report_path = output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    print(f"report={report_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
