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
from .watch_app_dispatch import WatchAppDispatchMixin
from .watch_app_state import WatchAppStateMixin
from .watch_app_utils import WatchAppUtilsMixin


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
    WatchAppDispatchMixin,
    WatchAppStateMixin,
    WatchAppUtilsMixin,
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
