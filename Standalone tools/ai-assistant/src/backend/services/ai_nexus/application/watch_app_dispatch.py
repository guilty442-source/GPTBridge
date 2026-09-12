from __future__ import annotations

import json
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from ..infrastructure.privacy import protect_text
from ..infrastructure.watch_repository import InvestmentWatchRepository


def local_device_now() -> datetime:
    return datetime.now(timezone.utc)


class WatchAppDispatchMixin:
    """Command dispatch and error-logging helpers for InvestmentWatchService."""

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        handlers = {
            "investment_watch_get_state": self._get_state,
            "investment_watch_read_portfolio_file": self._read_portfolio_file,
            "investment_watch_preview_excel_mapping": self._preview_excel_mapping,
            "investment_watch_import_excel_mapping": self._import_excel_mapping,
            "investment_watch_import_portfolio": self._import_portfolio,
            "investment_watch_clear_state": self._clear_state,
            "investment_watch_run_local_risk_ai": self._run_local_risk_ai,
            "investment_watch_export_report": self._export_report,
            "investment_watch_get_mobile_sync": self._get_mobile_sync,
            "investment_watch_set_mobile_sync_enabled": self._set_mobile_sync_enabled,
            "investment_watch_set_mobile_sync_remote_url": self._set_mobile_sync_remote_url,
            "investment_watch_rotate_mobile_sync_pairing": self._rotate_mobile_sync_pairing,
            "investment_watch_revoke_mobile_sync_pairing": self._revoke_mobile_sync_pairing,
            "investment_watch_get_analytics": self._get_analytics,
            "investment_watch_add_transaction": self._add_transaction,
            "investment_watch_seed_opening_ledger": self._seed_opening_ledger,
            "investment_watch_run_star_accounting": self._run_star_accounting,
            "investment_watch_delete_transaction": self._delete_transaction,
            "investment_watch_import_price_history": self._import_price_history,
            "investment_watch_sync_intelligence": self._sync_intelligence,
            "investment_watch_sync_open_markets": self._sync_open_markets,
            "investment_watch_sync_dividends": self._sync_dividends,
            "investment_watch_run_stress_test": self._run_stress_test,
            "investment_watch_run_backtest": self._run_backtest,
            "investment_watch_plan_rebalance": self._plan_rebalance,
            "investment_watch_add_event": self._add_event,
            "investment_watch_add_alert_rule": self._add_alert_rule,
            "investment_watch_acknowledge_alert": self._acknowledge_alert,
            "investment_watch_set_decision_status": self._set_decision_status,
            "investment_watch_update_v2_settings": self._update_v2_settings,
            "investment_watch_get_v3": self._get_v3,
            "investment_watch_add_fx_rates": self._add_fx_rates,
            "investment_watch_import_broker_statement": self._import_broker_statement,
            "investment_watch_approve_broker_rows": self._approve_broker_rows,
            "investment_watch_optimize_portfolio": self._optimize_portfolio,
            "investment_watch_run_monte_carlo": self._run_monte_carlo,
            "investment_watch_add_corporate_action": self._add_corporate_action,
            "investment_watch_review_corporate_action": self._review_corporate_action,
            "investment_watch_configure_scheduler": self._configure_scheduler,
            "investment_watch_run_scheduler": self._run_scheduler,
            "investment_watch_configure_notification": self._configure_notification,
            "investment_watch_dispatch_notifications": self._dispatch_notifications,
            "investment_watch_record_model_evaluation": self._record_model_evaluation,
            "investment_watch_create_backup": self._create_backup,
            "investment_watch_restore_backup": self._restore_backup,
            "investment_watch_rotate_database_key": self._rotate_database_key,
            "investment_watch_upsert_holding": self._upsert_holding,
            "investment_watch_delete_holding": self._delete_holding,
            "investment_watch_restore_portfolio_version": self._restore_portfolio_version,
            "investment_watch_resolve_fund_identities": self._resolve_fund_identities,
            "investment_watch_confirm_fund_identity": self._confirm_fund_identity,
            "investment_watch_reconcile_ledger": self._reconcile_ledger,
            "investment_watch_apply_ledger_reconciliation": self._apply_ledger_reconciliation,
            "investment_mobile_get_snapshot": self._investment_mobile_get_snapshot,
            "investment_mobile_submit_instruction": self._investment_mobile_submit_instruction,
        }
        if command not in handlers:
            return f"{command}_result", {
                "ok": False,
                "message": "投資管家不直接執行外部協作；投資分析一律由投資管家經 AI 通道處理。",
            }
        try:
            result = await handlers[command](payload)
        except Exception as exc:
            error_id = self._record_xingcheng_error(command, payload, exc)
            result = {
                "ok": False,
                "message": f"{exc}（錯誤代碼 {error_id}，已由投資管家自動記錄）",
                "error_id": error_id,
                "error_logged": True,
            }
        return f"{command}_result", result

    def _record_xingcheng_error(
        self,
        command: str,
        payload: dict[str, Any],
        exc: Exception,
    ) -> str:
        error_id = uuid.uuid4().hex[:12].upper()
        diagnostic_keys = {
            "trigger",
            "live_quotes",
            "period",
            "strategy",
            "channel_id",
            "enabled",
            "allow_lan",
            "label",
        }
        safe_payload = {
            str(key): InvestmentWatchRepository._shorten(str(value), 160)
            for key, value in payload.items()
            if str(key) in diagnostic_keys
        }
        record = {
            "error_id": error_id,
            "occurred_at": local_device_now().isoformat(),
            "command": command,
            "error_type": type(exc).__name__,
            "protection": "windows-dpapi-current-user-or-filesystem-permissions",
            "message_protected": protect_text(
                InvestmentWatchRepository._shorten(str(exc), 1200)
            ),
            "payload_protected": protect_text(
                json.dumps(safe_payload, ensure_ascii=False)
            ),
            "traceback_protected": protect_text(
                InvestmentWatchRepository._shorten(
                    "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                    8000,
                )
            ),
        }
        try:
            error_path = self.repository.runtime_root / "xingcheng-errors.jsonl"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            if error_path.exists() and error_path.stat().st_size >= 2 * 1024 * 1024:
                archive_root = error_path.parent / "xingcheng-error-archives"
                archive_root.mkdir(parents=True, exist_ok=True)
                stamp = local_device_now().strftime("%Y%m%d_%H%M%S_%f")
                archive_path = (
                    archive_root
                    / f"xingcheng-errors.{stamp}.{uuid.uuid4().hex[:8]}.jsonl"
                )
                error_path.replace(archive_path)
            with error_path.open("a", encoding="utf-8", newline="\n") as error_file:
                error_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            self.analytics_store.audit(
                "xingcheng_error",
                {
                    key: value
                    for key, value in record.items()
                    if key not in {"traceback_protected", "payload_protected"}
                },
            )
        except Exception:
            pass
        return error_id
