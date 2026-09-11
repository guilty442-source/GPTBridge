from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from ..infrastructure import portfolio_file as investment_manager_core
from ..infrastructure.analytics_repository import market_session_status


def local_device_now() -> datetime:
    return datetime.now(timezone.utc)


class WatchAppStateMixin:
    """State retrieval, analytics enrichment, and diagnostics for InvestmentWatchService."""

    async def _get_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = await asyncio.to_thread(self.repository.load_state)
        state_revision = self._state_revision(state)
        known_revision = str(payload.get("state_revision") or "").strip()
        if known_revision and known_revision == state_revision and not payload.get("force"):
            return {
                "ok": True,
                "version": self.VERSION,
                "not_modified": True,
                "state_revision": state_revision,
                "market_sessions": market_session_status(),
                "mobile_sync": self._mobile_sync_status(),
            }
        return await asyncio.to_thread(self._state_response, state)

    def _state_revision(self, state: dict[str, Any]) -> str:
        revision_parts = [
            str(state.get("updated_at") or "")
            if self.repository.state_path.exists()
            else "empty-state"
        ]
        paths = [
            self.repository.state_path,
            self.analytics_store.database_path,
            Path(f"{self.analytics_store.database_path}-wal"),
        ]
        for path in paths:
            try:
                revision_parts.append(f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}")
            except OSError:
                revision_parts.append(f"{path.name}:missing")
        return hashlib.sha256("|".join(revision_parts).encode("utf-8")).hexdigest()[:24]

    def _state_response(self, state: dict[str, Any]) -> dict[str, Any]:
        state = self._state_with_analytics(state)
        diagnostics = self._diagnostics(state)
        return {
            "ok": True,
            "version": self.VERSION,
            "state_revision": self._state_revision(state),
            "state": state,
            "diagnostics": diagnostics,
            "market_sessions": state.get("market_sessions"),
            "mobile_sync": self._mobile_sync_status(),
            "tool_root": str(self.tool_root),
            "state_path": str(self.repository.state_path),
            "analytics_path": str(self.analytics_store.database_path),
            "local_only": True,
            "safety": {
                "xingcheng": "投資管家本身不執行 AI 推理；所有投資分析均由投資管家經 AI 通道協調。",
                "storage": "狀態檔由 Windows DPAPI 使用目前帳號保護；分析資料庫的備註、事件來源與決策證據採欄位加密。",
                "quotes": "報價預設會自動連網抓取公開股價資料；輸入離線或不抓報價可改用本地資料評估。",
            },
        }

    def _state_with_analytics(self, state: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(state)
        enriched["portfolio_versions"] = self.repository.list_state_versions()
        portfolio = state.get("portfolio")
        if isinstance(portfolio, dict):
            enriched_portfolio = dict(portfolio)
            source_path = Path(str(portfolio.get("source_path") or ""))
            try:
                source_created_at = datetime.fromtimestamp(
                    source_path.stat().st_ctime
                ).astimezone().isoformat()
            except OSError:
                source_created_at = ""
            if source_created_at:
                enriched_portfolio["source_created_at"] = source_created_at
            enriched["portfolio"] = enriched_portfolio
        enriched["analytics"] = self.analytics_store.analytics_snapshot(state)
        enriched["v3"] = self._v3_snapshot(state)
        enriched["market_sessions"] = market_session_status()
        return enriched

    def _v3_snapshot(self, state: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        now = monotonic()
        if force or self._v3_snapshot_cache is None or now - self._v3_snapshot_cached_at > 60:
            snapshot = self.v3.snapshot(state)
            snapshot.update(
                {
                    "investment_policy": self._investment_policy(),
                    "broker_imports": self.broker_reconciliation.list_imports(),
                    "automation": self.automation.status(),
                    "notifications": {
                        "channels": self.notifications.list_channels(),
                        "outbox": self.notifications.outbox(30),
                    },
                    "model_governance": self.model_governance.dashboard(),
                    "database_security": self.analytics_store.database_security_status(),
                    "backups": self.analytics_store.list_backups()[:30],
                    "audit_log": self.analytics_store.list_audit_log(50),
                }
            )
            self._v3_snapshot_cache = snapshot
            self._v3_snapshot_cached_at = now
        return dict(self._v3_snapshot_cache)

    def _investment_policy(self) -> dict[str, Any]:
        return {
            "investment_goal": self.analytics_store.get_setting(
                "investment_goal", ""
            ),
            "time_horizon_years": self.analytics_store.get_setting(
                "time_horizon_years", 5
            ),
            "risk_capacity": self.analytics_store.get_setting(
                "risk_capacity", "balanced"
            ),
            "cash_need_percent": self.analytics_store.get_setting(
                "cash_need_percent", 0
            ),
            "forbidden_assets": self.analytics_store.get_setting(
                "forbidden_assets", []
            ),
            "target_return_percent": self.analytics_store.get_setting(
                "target_return_percent", None
            ),
            "human_approval_required": True,
            "automatic_order_submission": False,
        }

    def _diagnostics(self, state: dict[str, Any]) -> dict[str, Any]:
        holdings = state.get("holdings") if isinstance(state.get("holdings"), list) else []
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        workbook_scan = (
            state.get("workbook_scan") if isinstance(state.get("workbook_scan"), dict) else {}
        )
        workbook_quality = (
            state.get("workbook_scan_quality")
            if isinstance(state.get("workbook_scan_quality"), dict)
            else {}
        )
        product_status = (
            state.get("xingcheng_product_status")
            if isinstance(state.get("xingcheng_product_status"), dict)
            else {}
        )
        summary = (
            state.get("xingcheng_summary")
            if isinstance(state.get("xingcheng_summary"), dict)
            else {}
        )
        risk_warnings = [
            item
            for item in state.get("xingcheng_risk_warnings", [])
            if isinstance(item, dict)
        ]
        runs = [item for item in state.get("ai_runs", []) if isinstance(item, dict)]
        portfolio_age_hours = self._age_hours(portfolio.get("imported_at"))
        critical_count = max(
            self._int_value(summary.get("critical_count")),
            sum(1 for item in risk_warnings if item.get("severity") == "critical"),
        )
        warning_count = max(
            self._int_value(summary.get("warning_count")),
            sum(
                1
                for item in risk_warnings
                if item.get("severity") in {"critical", "warning"}
            ),
        )
        workbook_state = str(workbook_quality.get("state") or "")
        xingcheng_state = str(product_status.get("state") or "")
        stale = portfolio_age_hours is not None and portfolio_age_hours > 72
        symbols = [str(item.get("symbol") or "").strip() for item in holdings]
        decimal_symbol_count = sum(
            1
            for symbol in symbols
            if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[Ee][+-]?\d+)?", symbol)
        )
        invalid_symbol_count = sum(
            1
            for symbol in symbols
            if not investment_manager_core.looks_like_portfolio_symbol(symbol)
        )
        invalid_symbol_ratio = (
            invalid_symbol_count / len(symbols) if symbols else 0.0
        )
        mapping_error = bool(
            symbols
            and (
                decimal_symbol_count >= 3
                or (invalid_symbol_count >= 3 and invalid_symbol_ratio >= 0.2)
            )
        )

        if not holdings:
            state_key = "setup"
            state_label = "等待持股資料"
            message = "請讀取 Excel 持股檔，投資管家會經 AI 通道建立風險監測。"
        elif mapping_error:
            state_key = "critical"
            state_label = "Excel 欄位需修正"
            message = (
                f"Excel 欄位映射錯誤：{len(holdings)} 筆中有 {invalid_symbol_count} 筆代號"
                "看起來像價格或無效值；投資風險判讀已暫停，請用「Excel 欄位」重新匯入。"
            )
        elif critical_count > 0 or xingcheng_state == "critical" or workbook_state == "critical":
            state_key = "critical"
            state_label = "需要立即檢查"
            message = "已偵測重大風險或 Excel 掃描品質不足，請先確認資料與部位上限。"
        elif stale or warning_count > 0 or xingcheng_state == "attention" or workbook_state == "attention":
            state_key = "attention"
            state_label = "需要關注"
            message = "持股資料、投資管家分析或掃描品質有待確認項目，建議重新評估。"
        else:
            state_key = "ready"
            state_label = "監測正常"
            message = "投資管家已完成持股監測，資料狀態正常。"

        selected_sheet = (
            workbook_scan.get("selected_sheet")
            if isinstance(workbook_scan.get("selected_sheet"), dict)
            else {}
        )
        latest_run = runs[0] if runs else {}
        network_context = (
            state.get("xingcheng_network_context")
            if isinstance(state.get("xingcheng_network_context"), dict)
            else product_status.get("network_context")
            if isinstance(product_status.get("network_context"), dict)
            else {}
        )
        error_log_path = self.repository.runtime_root / "xingcheng-errors.jsonl"
        error_archive_root = (
            self.repository.runtime_root / "xingcheng-error-archives"
        )
        try:
            error_log_count = sum(
                1
                for line in error_log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError:
            error_log_count = 0
        try:
            error_archives = list(error_archive_root.glob("*.jsonl"))
            error_archive_bytes = sum(
                path.stat().st_size for path in error_archives
            )
        except OSError:
            error_archives = []
            error_archive_bytes = 0
        return {
            "state": state_key,
            "state_label": state_label,
            "message": message,
            "generated_at": local_device_now().isoformat(),
            "data_quality": {
                "state": "mapping_error" if mapping_error else "ready",
                "holding_count": len(holdings),
                "invalid_symbol_count": invalid_symbol_count,
                "decimal_symbol_count": decimal_symbol_count,
                "invalid_symbol_percent": round(invalid_symbol_ratio * 100.0, 2),
                "risk_analysis_suspended": mapping_error,
            },
            "portfolio": {
                "file_name": portfolio.get("file_name") or "",
                "holding_count": len(holdings),
                "imported_at": portfolio.get("imported_at") or "",
                "age_hours": portfolio_age_hours,
                "stale": stale,
            },
            "workbook": {
                "state": workbook_state or "not_applicable",
                "state_label": workbook_quality.get("state_label") or "非 Excel 或尚未掃描",
                "score": workbook_quality.get("score"),
                "sheet_count": workbook_scan.get("sheet_count") or 0,
                "selected_sheet_name": (
                    workbook_quality.get("selected_sheet_name")
                    or selected_sheet.get("sheet_name")
                    or ""
                ),
                "header_row_number": workbook_quality.get("header_row_number")
                or selected_sheet.get("header_row_number"),
                "header_depth": workbook_quality.get("header_depth")
                or selected_sheet.get("header_depth"),
                "valid_data_row_count": workbook_quality.get("valid_data_row_count")
                or selected_sheet.get("valid_data_row_count"),
                "recommendation": workbook_quality.get("recommendation") or "",
            },
            "xingcheng": {
                "state": xingcheng_state or ("empty" if not holdings else "attention"),
                "state_label": product_status.get("state_label")
                or ("等待持股資料" if not holdings else "等待投資管家分析"),
                "score": product_status.get("score"),
                "risk_level": product_status.get("risk_level"),
                "risk_level_label": product_status.get("risk_level_label"),
                "watch_status_label": product_status.get("watch_status_label"),
                "network_enabled": bool(product_status.get("network_enabled")),
                "network_mode": product_status.get("network_mode") or "",
                "network_mode_label": product_status.get("network_mode_label")
                or product_status.get("watch_status_label"),
                "quote_health": product_status.get("quote_health")
                or network_context.get("health")
                or "",
                "quote_health_label": product_status.get("quote_health_label")
                or network_context.get("health_label")
                or "",
                "network_policy": product_status.get("network_policy") or "",
                "quote_provider_count": network_context.get("quote_provider_count") or 0,
                "quote_providers": network_context.get("quote_providers") or [],
                "verified_quote_count": network_context.get("verified_quote_count") or 0,
                "cross_checked_count": network_context.get("cross_checked_count") or 0,
                "single_source_count": network_context.get("single_source_count") or 0,
                "untrusted_quote_count": network_context.get("untrusted_quote_count") or 0,
                "validation_issue_count": network_context.get("validation_issue_count") or 0,
                "divergence_count": network_context.get("divergence_count") or 0,
                "stale_quote_count": network_context.get("stale_quote_count") or 0,
                "symbol_mismatch_count": network_context.get("symbol_mismatch_count") or 0,
                "currency_mismatch_count": network_context.get("currency_mismatch_count") or 0,
                "quote_gap_count": network_context.get("quote_gap_count") or 0,
                "quote_gaps": network_context.get("quote_gaps") or [],
                "coverage_percent": network_context.get("coverage_percent"),
                "coverage_label": product_status.get("coverage_label") or "",
                "warning_count": warning_count,
                "critical_count": critical_count,
                "offline_mode": bool(product_status.get("offline_mode", True)),
                "generated_at": product_status.get("generated_at") or "",
            },
            "runs": {
                "count": len(runs),
                "latest": {
                    "run_id": latest_run.get("run_id") or "",
                    "role": latest_run.get("role") or "",
                    "provider": latest_run.get("provider") or "",
                    "status": latest_run.get("status") or "",
                    "created_at": latest_run.get("created_at") or "",
                },
            },
            "error_logging": {
                "enabled": True,
                "count": error_log_count,
                "path": str(error_log_path),
                "format": "jsonl",
                "archive_path": str(error_archive_root),
                "archive_file_count": len(error_archives),
                "archive_bytes": error_archive_bytes,
                "storage_pressure": error_archive_bytes >= 256 * 1024 * 1024,
                "automatic_delete": False,
            },
            "boundaries": {
                "local_only": True,
                "external_ai": False,
                "service_commands": sorted(self.COMMANDS),
            },
        }
