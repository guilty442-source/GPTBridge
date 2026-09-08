from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Iterator

from ..infrastructure import portfolio_file as investment_manager_core
from ..infrastructure.analytics_repository import (
    InvestmentAnalyticsStore,
    market_session_status,
    number,
)
from .automation import InvestmentAutomation, ModelGovernance, NotificationManager
from ..infrastructure.broker import BrokerReconciliationService
from ..infrastructure.privacy import encode_binary_document, protect_text
from ..infrastructure.watch_repository import InvestmentWatchRepository
from ..domain.contract import (
    INVESTMENT_APP_VERSION,
)
from .portfolio_engine import (
    InvestmentV3Engine,
)
from ..infrastructure.import_service import InvestmentImportServiceMixin
from .accounting_service import InvestmentAccountingServiceMixin
from .mobile_bridge import InvestmentMobileBridgeMixin
from .operations_service import InvestmentOperationsServiceMixin
from .portfolio_service import InvestmentPortfolioServiceMixin
from .star_service import InvestmentStarServiceMixin


TOOL_ROOT = Path(__file__).resolve().parents[4]




def local_device_now() -> datetime:
    return datetime.now().astimezone()

# Local parsing exports retained for the import service. Legacy market-provider
# entry points are intentionally not exposed: all market data arrives via Star.
Holding = investment_manager_core.Holding
InvestmentManagerError = investment_manager_core.InvestmentManagerError
QuoteProviderError = investment_manager_core.QuoteProviderError
load_portfolio = investment_manager_core.load_portfolio
load_json_portfolio = investment_manager_core.load_json_portfolio
load_csv_portfolio = investment_manager_core.load_csv_portfolio
load_xlsx_portfolio = investment_manager_core.load_xlsx_portfolio
load_xlsx_portfolio_with_mapping = investment_manager_core.load_xlsx_portfolio_with_mapping
load_xlsx_portfolio_horizontal_matrix = investment_manager_core.load_xlsx_portfolio_horizontal_matrix
load_xlsx_portfolio_consolidated_report = investment_manager_core.load_xlsx_portfolio_consolidated_report
scan_xlsx_workbook = investment_manager_core.scan_xlsx_workbook
xlsx_mapping_preview = investment_manager_core.xlsx_mapping_preview
create_snapshot = investment_manager_core.create_snapshot
market_status = investment_manager_core.market_status
utc_now = investment_manager_core.utc_now


def _resolve_tool_root(project_root: Path) -> Path:
    if project_root.name == "ai-assistant" and (project_root / "manifest.json").is_file():
        return project_root.resolve()
    candidate = project_root / "ai-assistant"
    if candidate.exists():
        return candidate.resolve()
    return project_root.resolve()


class InvestmentWatchService(
    InvestmentImportServiceMixin,
    InvestmentAccountingServiceMixin,
    InvestmentStarServiceMixin,
    InvestmentMobileBridgeMixin,
    InvestmentPortfolioServiceMixin,
    InvestmentOperationsServiceMixin,
):
    VERSION = INVESTMENT_APP_VERSION
    IMPORT_SNAPSHOT_KEEP = 20
    IMPORT_INLINE_WAIT_SECONDS = 15.0
    IMPORT_SHUTDOWN_DRAIN_SECONDS = 3.0
    COMMANDS = {
        "investment_watch_get_state",
        "investment_watch_read_portfolio_file",
        "investment_watch_preview_excel_mapping",
        "investment_watch_import_excel_mapping",
        "investment_watch_import_portfolio",
        "investment_watch_clear_state",
        "investment_watch_run_local_risk_ai",
        "investment_watch_export_report",
        "investment_watch_get_mobile_sync",
        "investment_watch_set_mobile_sync_enabled",
        "investment_watch_set_mobile_sync_remote_url",
        "investment_watch_rotate_mobile_sync_pairing",
        "investment_watch_revoke_mobile_sync_pairing",
        "investment_watch_get_analytics",
        "investment_watch_add_transaction",
        "investment_watch_seed_opening_ledger",
        "investment_watch_run_star_accounting",
        "investment_watch_delete_transaction",
        "investment_watch_import_price_history",
        "investment_watch_sync_intelligence",
        "investment_watch_sync_open_markets",
        "investment_watch_sync_dividends",
        "investment_watch_run_stress_test",
        "investment_watch_run_backtest",
        "investment_watch_plan_rebalance",
        "investment_watch_add_event",
        "investment_watch_add_alert_rule",
        "investment_watch_acknowledge_alert",
        "investment_watch_set_decision_status",
        "investment_watch_update_v2_settings",
        "investment_watch_get_v3",
        "investment_watch_add_fx_rates",
        "investment_watch_import_broker_statement",
        "investment_watch_approve_broker_rows",
        "investment_watch_optimize_portfolio",
        "investment_watch_run_monte_carlo",
        "investment_watch_add_corporate_action",
        "investment_watch_review_corporate_action",
        "investment_watch_configure_scheduler",
        "investment_watch_run_scheduler",
        "investment_watch_configure_notification",
        "investment_watch_dispatch_notifications",
        "investment_watch_record_model_evaluation",
        "investment_watch_create_backup",
        "investment_watch_restore_backup",
        "investment_watch_rotate_database_key",
        "investment_watch_upsert_holding",
        "investment_watch_delete_holding",
        "investment_watch_restore_portfolio_version",
        "investment_watch_resolve_fund_identities",
        "investment_watch_confirm_fund_identity",
        "investment_watch_reconcile_ledger",
        "investment_watch_apply_ledger_reconciliation",
        "investment_mobile_get_snapshot",
        "investment_mobile_submit_instruction",
    }

    def __init__(
        self,
        project_root: Path,
        browser_session: Any | None = None,
        *,
        ai_connections: Any | None = None,
    ) -> None:
        del browser_session
        self.project_root = project_root.resolve()
        self.tool_root = _resolve_tool_root(project_root)
        self.ai_connections = ai_connections
        self.repository = InvestmentWatchRepository(self.tool_root)
        try:
            self.analytics_store = InvestmentAnalyticsStore(self.tool_root)
        except BaseException:
            try:
                self.repository.close()
            except BaseException:
                pass
            raise
        try:
            self.v3 = InvestmentV3Engine(self.analytics_store)
            self.broker_reconciliation = BrokerReconciliationService(self.analytics_store)
            self.notifications = NotificationManager(self.analytics_store)
            self.model_governance = ModelGovernance(self.analytics_store)
            self.automation = InvestmentAutomation(
                self.analytics_store,
                self.notifications,
                self._run_automation_cycle,
                self._coordinated_backup,
            )
            self._v3_snapshot_cache: dict[str, Any] | None = None
            self._v3_snapshot_cached_at = 0.0
            self._background_jobs: set[asyncio.Task[Any]] = set()
            self._star_accounting_task: asyncio.Task[dict[str, Any]] | None = None
            self._import_operation_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
            self._durable_worker_tasks: set[asyncio.Task[dict[str, Any]]] = set()
            self._active_daemon_workers: set[str] = set()
            self._daemon_worker_lock = threading.Lock()
            self._import_shutdown_event = threading.Event()
            self._storage_closed = False
            self._shutdown_started = False
            self._shutdown_cleanup_ready = False
            self._event_loop: asyncio.AbstractEventLoop | None = None
            self._mobile_sync_start_error = ""
        except BaseException:
            try:
                self.analytics_store.close()
            except BaseException:
                pass
            try:
                self.repository.close()
            except BaseException:
                pass
            raise

    @property
    def workspace(self) -> Any:
        tool_root = self.tool_root

        class Workspace:
            workspace_root = tool_root

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._shutdown_started = False
        self._shutdown_cleanup_ready = False
        self._import_shutdown_event.clear()
        if self.analytics_store.get_setting("mobile_sync_enabled", None) is None:
            self.analytics_store.set_setting("mobile_sync_enabled", False)
        if self.analytics_store.get_setting("mobile_sync_allow_lan", None) is None:
            self.analytics_store.set_setting("mobile_sync_allow_lan", False)
        if self.analytics_store.get_setting("mobile_sync_port", None) is None:
            self.analytics_store.set_setting("mobile_sync_port", 18765)
        self._mobile_sync_start_error = ""
        await self.automation.start()
        self._resume_import_operations()
        current_state = self.repository.load_state()
        if current_state.get("holdings") and not self.repository.list_state_versions():
            self.repository.create_state_version(
                current_state,
                reason="upgrade_baseline",
            )
        recovered_runs = self.repository.recover_interrupted_ai_runs()
        if recovered_runs:
            state = self.repository.load_state()
            if state.get("holdings") and not state.get("xingcheng_product_status"):
                self._schedule_local_risk_ai_background(
                    state,
                    {"trigger": "interrupted_run_recovery", "live_quotes": True},
                )
        if current_state.get("holdings"):
            self._schedule_star_accounting_background(trigger="startup")
        return None

    async def shutdown(self) -> None:
        self._shutdown_started = True
        self._import_shutdown_event.set()
        await self.automation.stop()
        shutdown_deadline = (
            monotonic() + self.IMPORT_SHUTDOWN_DRAIN_SECONDS
        )
        active_imports = {
            operation_id: task
            for operation_id, task in self._import_operation_tasks.items()
            if not task.done()
        }
        if active_imports:
            _done, pending = await asyncio.wait(
                set(active_imports.values()),
                timeout=max(0.0, shutdown_deadline - monotonic()),
            )
            pending_tasks = set(pending)
            for operation_id, task in active_imports.items():
                if task not in pending_tasks:
                    continue
                try:
                    self.analytics_store.update_import_operation(
                        operation_id,
                        status="resume_pending",
                        reason="bounded_shutdown_drain_expired",
                    )
                except Exception:
                    pass
        active_durable_workers = {
            task for task in self._durable_worker_tasks if not task.done()
        }
        if active_durable_workers:
            await asyncio.wait(
                active_durable_workers,
                timeout=max(0.0, shutdown_deadline - monotonic()),
            )
        active_background_jobs = {
            task for task in self._background_jobs if not task.done()
        }
        if active_background_jobs:
            await asyncio.wait(
                active_background_jobs,
                timeout=max(0.0, shutdown_deadline - monotonic()),
            )
        self._shutdown_cleanup_ready = True
        self._maybe_close_storage()
        return None

    def _close_storage(self) -> None:
        if self._storage_closed:
            return
        self.analytics_store.close()
        self.repository.close()
        self._storage_closed = True

    def _daemon_workers_active(self) -> bool:
        with self._daemon_worker_lock:
            return bool(self._active_daemon_workers)

    def _maybe_close_storage(self) -> None:
        if (
            self._shutdown_started
            and self._shutdown_cleanup_ready
            and not self._import_operation_tasks
            and not self._durable_worker_tasks
            and not self._background_jobs
            and not self._daemon_workers_active()
        ):
            self._close_storage()

    async def _await_daemon_worker(
        self,
        worker: Any,
        *args: Any,
        worker_name: str,
    ) -> dict[str, Any]:
        """Run blocking file/DB work without keeping interpreter shutdown alive."""

        loop = asyncio.get_running_loop()
        completion: asyncio.Future[dict[str, Any]] = loop.create_future()
        completion.add_done_callback(
            lambda finished: (
                None if finished.cancelled() else finished.exception()
            )
        )
        worker_id = f"{worker_name}-{uuid.uuid4().hex}"
        with self._daemon_worker_lock:
            self._active_daemon_workers.add(worker_id)

        def finish_on_loop(
            result: dict[str, Any] | None,
            error: Exception | None,
        ) -> None:
            with self._daemon_worker_lock:
                self._active_daemon_workers.discard(worker_id)
            if not completion.done():
                if error is not None:
                    completion.set_exception(error)
                else:
                    completion.set_result(result or {})
            self._maybe_close_storage()

        def invoke() -> None:
            result: dict[str, Any] | None = None
            error: Exception | None = None
            try:
                result = worker(*args)
            except Exception as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(finish_on_loop, result, error)
            except RuntimeError:
                with self._daemon_worker_lock:
                    self._active_daemon_workers.discard(worker_id)
                if self._shutdown_started and not self._daemon_workers_active():
                    self._close_storage()

        threading.Thread(
            target=invoke,
            name=f"gptbridge-{worker_name}",
            daemon=True,
        ).start()
        return await asyncio.shield(completion)

    @contextmanager
    def _snapshot_coordinator(self) -> Iterator[dict[str, Any]]:
        """Acquire cross-file locks in the only supported order."""

        with self.repository.exclusive_data_access():
            with self.analytics_store.exclusive_data_access():
                yield self.repository.load_state()

    def _coordinated_backup(self, label: str) -> dict[str, Any]:
        with self._snapshot_coordinator():
            return self.analytics_store.backup_database(label)

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
                "message": "AI投資管家不直接執行外部協作；投資分析一律由AI投資管家經 AI 通道處理。",
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
                "xingcheng": "AI投資管家本身不執行 AI 推理；所有投資分析均由AI投資管家經 AI 通道協調。",
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
            message = "請讀取 Excel 持股檔，AI投資管家會經 AI 通道建立風險監測。"
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
            message = "持股資料、AI投資管家分析或掃描品質有待確認項目，建議重新評估。"
        else:
            state_key = "ready"
            state_label = "監測正常"
            message = "AI投資管家已完成持股監測，資料狀態正常。"

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
                or ("等待持股資料" if not holdings else "等待AI投資管家分析"),
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
