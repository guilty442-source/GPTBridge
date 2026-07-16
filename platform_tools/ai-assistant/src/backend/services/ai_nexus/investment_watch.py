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

from . import investment_manager_core
from . import local_risk_ai
from .investment_analytics import (
    InvestmentAnalyticsStore,
    market_session_status,
    number,
    sync_yahoo_dividends,
    sync_yahoo_intelligence,
    sync_yahoo_open_market_quotes,
)
from .investment_automation import InvestmentAutomation, ModelGovernance, NotificationManager
from .investment_broker import BrokerReconciliationService
from .investment_local_ai_upgrade import (
    FundIdentityResolver,
    LocalExplanationEngine,
    build_smart_mapping_repair,
)
from .investment_privacy import encode_binary_document, protect_text
from .investment_repository import InvestmentWatchRepository
from .investment_contract import (
    INVESTMENT_APP_VERSION,
)
from .investment_v3 import (
    InvestmentV3Engine,
    sync_factor_proxies_from_yahoo,
    sync_fx_from_huanan_bank,
)
from .mobile_sync import (
    MobileSyncGateway,
    mobile_platform_contract,
    normalize_remote_url,
)


TOOL_ROOT = Path(__file__).resolve().parents[4]


class _ImportResumePending(RuntimeError):
    """Internal signal: parsing may be retried, but no state commit was made."""


def local_device_now() -> datetime:
    return datetime.now().astimezone()

# Compatibility exports for older dynamic loaders that treated the investment
# watch service module as the former investment manager entry.
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
run_manager = investment_manager_core.run_manager
provider_registry = investment_manager_core.provider_registry
quote_holding = investment_manager_core.quote_holding
market_status = investment_manager_core.market_status
utc_now = investment_manager_core.utc_now
main = investment_manager_core.main


def _resolve_tool_root(project_root: Path) -> Path:
    candidate = project_root / "platform_tools" / "ai-assistant"
    if candidate.exists():
        return candidate.resolve()
    return project_root.resolve()


class InvestmentWatchService:
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
            self.fund_identity_resolver = FundIdentityResolver()
            self.local_explanation_engine = LocalExplanationEngine()
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
            self.mobile_sync_gateway = MobileSyncGateway(
                self.tool_root,
                self._mobile_sync_snapshot,
                self._schedule_mobile_local_ai_command,
                self._mobile_sync_remote_url,
            )
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
        port_value = str(os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_PORT") or "").strip()
        port = int(port_value) if port_value.isdigit() else None
        explicit_enabled = str(
            os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ENABLED") or ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        mobile_enabled = (
            explicit_enabled
            or bool(port_value)
            or bool(self.analytics_store.get_setting("mobile_sync_enabled", False))
        )
        allow_lan = str(
            os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_ALLOW_LAN") or ""
        ).strip().casefold() in {"1", "true", "yes", "on"} or bool(
            self.analytics_store.get_setting("mobile_sync_allow_lan", False)
        )
        if mobile_enabled:
            try:
                self.mobile_sync_gateway.start(port=port, allow_lan=allow_lan)
            except OSError as exc:
                self._mobile_sync_start_error = str(exc)
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
            if state.get("holdings") and not state.get("local_ai_product_status"):
                self._schedule_local_risk_ai_background(
                    state,
                    {"trigger": "interrupted_run_recovery", "live_quotes": True},
                )
        return None

    async def shutdown(self) -> None:
        self._shutdown_started = True
        self._import_shutdown_event.set()
        await self.automation.stop()
        self.mobile_sync_gateway.shutdown()
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
        }
        if command not in handlers:
            return f"{command}_result", {
                "ok": False,
                "message": "AI投資管家僅支援本地輔助 AI；外部 AI 協作請使用 AI協作工具。",
            }
        try:
            result = await handlers[command](payload)
        except Exception as exc:
            error_id = self._record_local_ai_error(command, payload, exc)
            result = {
                "ok": False,
                "message": f"{exc}（錯誤代碼 {error_id}，已由本地 AI 自動記錄）",
                "error_id": error_id,
                "error_logged": True,
            }
        return f"{command}_result", result

    def _record_local_ai_error(
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
            error_path = self.repository.runtime_root / "local-ai-errors.jsonl"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            if error_path.exists() and error_path.stat().st_size >= 2 * 1024 * 1024:
                archive_root = error_path.parent / "local-ai-error-archives"
                archive_root.mkdir(parents=True, exist_ok=True)
                stamp = local_device_now().strftime("%Y%m%d_%H%M%S_%f")
                archive_path = (
                    archive_root
                    / f"local-ai-errors.{stamp}.{uuid.uuid4().hex[:8]}.jsonl"
                )
                error_path.replace(archive_path)
            with error_path.open("a", encoding="utf-8", newline="\n") as error_file:
                error_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            self.analytics_store.audit(
                "local_ai_error",
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
                "local_ai": "此工具僅執行本地輔助 AI 與投資看盤，不連接外部 AI 協作流程。",
                "storage": "狀態檔由 Windows DPAPI 使用目前帳號保護；分析資料庫的備註、事件來源與決策證據採欄位加密。",
                "quotes": "報價預設會自動連網抓取公開股價資料；輸入離線或不抓報價可改用本地資料評估。",
            },
        }

    async def _get_mobile_sync(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return self._state_response(self.repository.load_state())

    async def _set_mobile_sync_enabled(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("enabled"))
        allow_lan = bool(payload.get("allow_lan"))
        port_value = str(payload.get("port") or "").strip()
        port = int(port_value) if port_value.isdigit() else None
        self.analytics_store.set_setting("mobile_sync_enabled", enabled)
        self.analytics_store.set_setting("mobile_sync_allow_lan", allow_lan)
        if enabled:
            self.mobile_sync_gateway.rotate_pairing_code()
            self.mobile_sync_gateway.start(port=port, allow_lan=allow_lan)
            self._mobile_sync_start_error = ""
        else:
            self.mobile_sync_gateway.revoke_pairing_code()
            self.mobile_sync_gateway.shutdown()
        response = self._state_response(self.repository.load_state())
        response["message"] = (
            "手機同步已啟用；區網存取已明確開放。"
            if enabled and allow_lan
            else "手機同步已啟用，僅限本機。"
            if enabled
            else "手機同步已關閉。"
        )
        return response

    async def _set_mobile_sync_remote_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_url = payload.get("remote_url") or payload.get("remote_base_url") or ""
        remote_url = normalize_remote_url(raw_url) if str(raw_url or "").strip() else ""
        state = self.repository.save_mobile_sync_remote_url(remote_url)
        return {
            "ok": True,
            "message": "不同網路同步橋接已更新" if remote_url else "不同網路同步橋接已清空",
            "state": state,
            "diagnostics": self._diagnostics(state),
            "mobile_sync": self._mobile_sync_status(),
        }

    async def _rotate_mobile_sync_pairing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self.mobile_sync_gateway.rotate_pairing_code()
        state = self.repository.load_state()
        return {
            "ok": True,
            "message": "手機同步配對碼已更新",
            "state": state,
            "diagnostics": self._diagnostics(state),
            "mobile_sync": self._mobile_sync_status(),
        }

    async def _revoke_mobile_sync_pairing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        self.mobile_sync_gateway.revoke_pairing_code()
        response = self._state_response(self.repository.load_state())
        response["message"] = "手機同步配對已撤銷；更新配對碼後才可再次連線。"
        return response

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

    async def _get_analytics(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return self._state_response(self.repository.load_state())

    async def _add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction = self.analytics_store.add_transaction(payload)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "交易已寫入本機帳本。", "transaction": transaction})
        return response

    async def _seed_opening_ledger(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            portfolio = (
                state.get("portfolio")
                if isinstance(state.get("portfolio"), dict)
                else {}
            )
            occurred_at = str(payload.get("occurred_at") or "").strip()
            if not occurred_at:
                source_path = Path(str(portfolio.get("source_path") or ""))
                try:
                    occurred_at = datetime.fromtimestamp(
                        source_path.stat().st_ctime
                    ).astimezone().isoformat()
                except OSError:
                    occurred_at = str(portfolio.get("imported_at") or "")
            safety_backup = self.analytics_store.backup_database(
                "before-opening-ledger"
            )
            result = self.analytics_store.sync_opening_balance_transactions(
                state,
                occurred_at,
            )
            self._invalidate_v3_snapshot()
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"已建立 {result.get('generated_count', 0)} 筆估算期初交易；"
                    f"覆蓋 {result.get('coverage_percent', 0)}%，XIRR 已改用新台幣基準。"
                ),
                "opening_ledger": result,
                "safety_backup": safety_backup,
            }
        )
        return response

    def _enrich_holding_principal_basis(
        self,
        holding: dict[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(holding)
        quantity = number(holding.get("quantity"), 0)
        principal_amount = number(
            holding.get("principal_amount"),
            number(holding.get("average_cost"), 0) * quantity,
        )
        principal_currency = str(
            holding.get("principal_currency")
            or holding.get("currency")
            or "TWD"
        ).upper()
        principal_fx = self.v3.fx_rate(principal_currency, "TWD")
        if (
            principal_amount > 0
            and principal_fx
            and number(principal_fx.get("rate"), 0) > 0
        ):
            enriched["principal_amount"] = principal_amount
            enriched["principal_currency"] = principal_currency
            enriched["principal_twd"] = round(
                principal_amount * number(principal_fx.get("rate")),
                4,
            )
            enriched["principal_fx_provider"] = str(
                principal_fx.get("provider") or ""
            )
            enriched["principal_fx_rate"] = number(principal_fx.get("rate"))
        asset_currency = str(holding.get("currency") or "TWD").upper()
        asset_fx = self.v3.fx_rate(asset_currency, "TWD")
        principal_twd = number(enriched.get("principal_twd"), 0)
        if (
            asset_currency != principal_currency
            and quantity > 0
            and principal_twd > 0
            and asset_fx
            and number(asset_fx.get("rate"), 0) > 0
        ):
            enriched["average_cost"] = round(
                principal_twd / number(asset_fx.get("rate")) / quantity,
                8,
            )
            enriched["average_cost_currency"] = asset_currency
            enriched["average_cost_method"] = (
                "principal_twd_huanan_current_fx_estimate"
            )
            enriched["average_cost_fx_rate"] = number(asset_fx.get("rate"))
        return enriched

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
        def optional_number(field: str) -> float | None:
            raw_value = payload.get(field, (existing or {}).get(field))
            if raw_value is None or raw_value == "":
                return None
            parsed = number(raw_value, -1)
            if parsed < 0:
                raise ValueError(f"{field} 不可小於零")
            return parsed

        dividend_amount_twd = optional_number("dividend_amount_twd")
        dividend_per_unit = optional_number("dividend_per_unit")
        monthly_dividend_twd = optional_number("monthly_dividend_twd")
        annual_dividend_yield_percent = optional_number(
            "annual_dividend_yield_percent"
        )
        payback_rate_percent = optional_number("payback_rate_percent")
        current_value_twd = optional_number("current_value_twd")
        principal_amount = optional_number("principal_amount")
        principal_twd = optional_number("principal_twd")
        principal_currency = str(
            payload.get("principal_currency")
            or (existing or {}).get("principal_currency")
            or currency
        ).strip().upper()
        fund_code = str(
            payload.get("fund_code") or (existing or {}).get("fund_code") or ""
        ).strip().upper()
        fund_isin = str(
            payload.get("fund_isin") or (existing or {}).get("fund_isin") or ""
        ).strip().upper()
        fund_share_class = str(
            payload.get("fund_share_class")
            or (existing or {}).get("fund_share_class")
            or ""
        ).strip()
        fund_quote_symbol = str(
            payload.get("fund_quote_symbol")
            or (existing or {}).get("fund_quote_symbol")
            or ""
        ).strip().upper()
        estimated_annual_dividend_twd = (
            monthly_dividend_twd * 12.0
            if monthly_dividend_twd is not None and monthly_dividend_twd > 0
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
        if not symbol:
            raise ValueError("持股代號不可空白")
        if quantity < 0:
            raise ValueError("持股數量不可小於零")
        if average_cost < 0:
            raise ValueError("平均成本不可小於零")
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
        return {
            **(existing or {}),
            "holding_id": str((existing or {}).get("holding_id") or payload.get("holding_id") or ""),
            "symbol": symbol,
            "name": str(payload.get("name") or symbol).strip(),
            "market": market,
            "asset_type": asset_type,
            "quantity": quantity,
            "average_cost": average_cost,
            "currency": currency,
            "principal_amount": principal_amount,
            "principal_currency": principal_currency,
            "principal_twd": principal_twd,
            "fund_code": fund_code,
            "fund_isin": fund_isin,
            "fund_share_class": fund_share_class,
            "fund_quote_symbol": fund_quote_symbol,
            "fund_identity_status": (
                "confirmed"
                if fund_quote_symbol
                else str((existing or {}).get("fund_identity_status") or "")
            ),
            "source_row": (existing or {}).get("source_row"),
            "dividend_amount_twd": dividend_amount_twd,
            "dividend_per_unit": dividend_per_unit,
            "monthly_dividend_twd": monthly_dividend_twd,
            "annual_dividend_yield_percent": annual_dividend_yield_percent,
            "payback_rate_percent": payback_rate_percent,
            "current_value_twd": current_value_twd,
            "estimated_annual_dividend_twd": estimated_annual_dividend_twd,
            "estimated_weekly_dividend_twd": estimated_weekly_dividend_twd,
            "manually_edited": True,
        }

    async def _upsert_holding(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        holding_id = str(payload.get("holding_id") or "").strip()
        target_index = next(
            (index for index, item in enumerate(holdings) if str(item.get("holding_id") or "") == holding_id),
            None,
        ) if holding_id else None
        if holding_id and target_index is None:
            raise ValueError("找不到要修改的持股")
        existing = holdings[target_index] if target_index is not None else None
        normalized = self._normalized_manual_holding(payload, existing)
        duplicate = next(
            (
                item
                for index, item in enumerate(holdings)
                if index != target_index
                and str(item.get("symbol") or "").upper() == normalized["symbol"]
                and str(item.get("market") or "").upper() == normalized["market"]
            ),
            None,
        )
        if duplicate:
            raise ValueError("同一市場已有相同持股代號")
        action = "update" if target_index is not None else "create"
        if target_index is None:
            holdings.append(normalized)
        else:
            holdings[target_index] = normalized
        saved = self.repository.replace_holdings(
            holdings,
            change={"action": action, "symbol": normalized["symbol"]},
        )
        saved_holding = next(
            (
                item
                for item in saved.get("holdings", [])
                if item.get("symbol") == normalized["symbol"] and item.get("market") == normalized["market"]
            ),
            normalized,
        )
        self.analytics_store.audit(
            "holding_manually_updated",
            {
                "action": action,
                "holding_id": saved_holding.get("holding_id"),
                "before": existing,
                "after": saved_holding,
                "source_file_modified": False,
            },
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        if payload.get("refresh_quotes", True) and saved.get("holdings"):
            self._schedule_local_risk_ai_background(
                saved,
                {"trigger": "manual_holding_change", "live_quotes": True},
            )
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": "持股已更新；原始 Excel 未被修改。" if action == "update" else "持股已新增；資料只保存於本機。",
                "holding": saved_holding,
                "source_file_modified": False,
            }
        )
        return response

    async def _delete_holding(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("刪除持股前必須明確確認")
        holding_id = str(payload.get("holding_id") or "").strip()
        if not holding_id:
            raise ValueError("缺少 holding_id")
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        removed = next((item for item in holdings if str(item.get("holding_id") or "") == holding_id), None)
        if not removed:
            raise ValueError("找不到要刪除的持股")
        remaining = [item for item in holdings if str(item.get("holding_id") or "") != holding_id]
        self.repository.replace_holdings(
            remaining,
            change={"action": "delete", "symbol": removed.get("symbol")},
        )
        self.analytics_store.audit(
            "holding_manually_deleted",
            {"holding": removed, "source_file_modified": False},
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": "持股已從本機清單刪除；原始 Excel 未被修改。",
                "deleted": True,
                "source_file_modified": False,
            }
        )
        return response

    async def _restore_portfolio_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("還原持股版本前必須明確確認")
        version_id = str(payload.get("version_id") or "").strip()
        restored = self.repository.restore_state_version(version_id)
        self.analytics_store.audit(
            "portfolio_version_restored",
            {"version_id": version_id, "holding_count": len(restored.get("holdings", []))},
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        if restored.get("holdings"):
            self._schedule_local_risk_ai_background(
                restored,
                {"trigger": "portfolio_version_restore", "live_quotes": True},
            )
        response = self._state_response(self.repository.load_state())
        response["message"] = f"已還原持股版本 {version_id}，並保留還原前版本。"
        return response

    async def _resolve_fund_identities(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        resolved, summary = await asyncio.to_thread(
            self.fund_identity_resolver.resolve_holdings,
            holdings,
            limit=self._int_value(payload.get("limit")) or 50,
        )
        saved = self.repository.replace_holdings(
            resolved,
            change={"action": "fund_identity_resolution", "matched": summary.get("matched_count", 0)},
        )
        saved["fund_identity_sync"] = summary
        self.repository.save_state(saved)
        self.analytics_store.audit("fund_identity_resolution", summary)
        self._invalidate_v3_snapshot()
        response = self._state_response(saved)
        response.update(
            {
                "message": (
                    f"共同基金辨識完成：比對 {summary.get('attempted_count', 0)} 筆，"
                    f"找到 {summary.get('matched_count', 0)} 筆，高信心自動確認 "
                    f"{summary.get('auto_confirmed_count', 0)} 筆。"
                ),
                "fund_identity_sync": summary,
            }
        )
        return response

    async def _confirm_fund_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        holding_id = str(payload.get("holding_id") or "").strip()
        quote_symbol = str(payload.get("quote_symbol") or "").strip().upper()
        if not holding_id or not quote_symbol:
            raise ValueError("缺少基金持股或報價代號")
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        target = next(
            (item for item in holdings if str(item.get("holding_id") or "") == holding_id),
            None,
        )
        if target is None:
            raise ValueError("找不到共同基金持股")
        candidates = target.get("fund_identity_candidates") if isinstance(target.get("fund_identity_candidates"), list) else []
        candidate = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and str(item.get("symbol") or "").upper() == quote_symbol
            ),
            None,
        )
        target["fund_quote_symbol"] = quote_symbol
        target["fund_candidate_symbol"] = quote_symbol
        target["fund_identity_status"] = "confirmed"
        target["fund_identity_confirmation"] = "manual"
        if isinstance(candidate, dict):
            target["fund_identity_confidence"] = candidate.get("confidence_score")
            target["fund_identity_source"] = candidate.get("source")
            target["fund_identity_source_url"] = candidate.get("source_url")
        saved = self.repository.replace_holdings(
            holdings,
            change={"action": "fund_identity_confirm", "symbol": target.get("symbol")},
        )
        self.analytics_store.audit(
            "fund_identity_confirmed",
            {"holding_id": holding_id, "symbol": target.get("symbol"), "quote_symbol": quote_symbol},
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        response = self._state_response(saved)
        response["message"] = f"已確認基金報價代號 {quote_symbol}。"
        return response

    async def _reconcile_ledger(self, _payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            reconciliation = self.analytics_store.reconcile_ledger_holdings(state)
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"帳本對帳完成：{reconciliation.get('matched_count', 0)} 筆相符，"
                    f"{reconciliation.get('difference_count', 0)} 筆差異。"
                ),
                "ledger_reconciliation": reconciliation,
            }
        )
        return response

    async def _apply_ledger_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            safety_backup = self.analytics_store.backup_database(
                "before-ledger-reconciliation"
            )
            reconciliation = self.analytics_store.apply_ledger_reconciliation(
                state,
                confirmed=payload.get("confirmed") is True,
            )
            self._invalidate_v3_snapshot()
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"已建立 {reconciliation.get('applied_count', 0)} 筆估算對帳調整；"
                    "原始持股與券商檔案均未修改。"
                ),
                "ledger_reconciliation": reconciliation,
                "safety_backup": safety_backup,
            }
        )
        return response

    async def _delete_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(payload.get("transaction_id") or "").strip()
        if not transaction_id:
            raise ValueError("缺少 transaction_id")
        deleted = self.analytics_store.delete_transaction(transaction_id)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "交易已刪除。" if deleted else "找不到交易。", "deleted": deleted})
        return response

    async def _import_price_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise ValueError("bars 必須是歷史行情陣列")
        count = self.analytics_store.add_price_bars(
            item for item in raw_bars if isinstance(item, dict)
        )
        response = self._state_response(self.repository.load_state())
        response.update({"message": f"已匯入 {count} 筆歷史行情。", "price_count": count})
        return response

    async def _sync_intelligence(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        if not holdings:
            raise ValueError("請先讀取持股檔，再同步市場情報")
        period = str(payload.get("period") or "1y")
        result = await asyncio.to_thread(
            sync_yahoo_intelligence,
            self.analytics_store,
            holdings,
            period=period,
        )
        currencies = [
            str(currency or "")
            for item in holdings
            for currency in (
                item.get("currency"),
                item.get("principal_currency"),
            )
        ]
        fx_result = await asyncio.to_thread(
            sync_fx_from_huanan_bank,
            self.v3,
            currencies,
            str(self.analytics_store.get_setting("base_currency", "TWD") or "TWD"),
        )
        result.update(fx_result)
        factor_result = await asyncio.to_thread(
            sync_factor_proxies_from_yahoo,
            self.v3,
            period="2y",
        )
        result.update(factor_result)
        result["corporate_actions_added"] = self._ingest_corporate_actions_from_events()
        self._invalidate_v3_snapshot()
        response = self._state_response(state)
        response.update(result)
        return response

    @staticmethod
    def _holding_merge_key(holding: dict[str, Any]) -> str:
        holding_id = str(holding.get("holding_id") or "").strip()
        if holding_id:
            return f"id:{holding_id}"
        return (
            f"symbol:{str(holding.get('market') or '').strip().upper()}|"
            f"{str(holding.get('symbol') or '').strip().upper()}"
        )

    def _merge_synchronized_holdings(
        self,
        original_holdings: list[dict[str, Any]],
        synchronized_holdings: list[dict[str, Any]],
        *,
        sync_metadata: dict[str, Any],
        sync_owned_fields: set[str],
    ) -> dict[str, Any]:
        """Three-way merge network enrichment without reverting manual edits."""

        original_by_key = {
            self._holding_merge_key(item): item for item in original_holdings
        }
        synchronized_by_key = {
            self._holding_merge_key(item): item for item in synchronized_holdings
        }

        def mutate(latest_state: dict[str, Any]) -> None:
            merged: list[dict[str, Any]] = []
            for current_item in latest_state.get("holdings", []):
                if not isinstance(current_item, dict):
                    continue
                key = self._holding_merge_key(current_item)
                original = original_by_key.get(key)
                synchronized = synchronized_by_key.get(key)
                if original is None or synchronized is None:
                    # Newly added holdings remain untouched; deleted holdings are
                    # absent from latest_state and therefore cannot be resurrected.
                    merged.append(dict(current_item))
                    continue
                current = dict(current_item)
                for field, value in synchronized.items():
                    if field == "holding_id" or value == original.get(field):
                        continue
                    if field in sync_owned_fields or current.get(field) == original.get(field):
                        current[field] = value
                merged.append(current)
            latest_state["holdings"] = merged
            latest_state.update(sync_metadata)

        return self.repository.update_state(mutate)

    async def _sync_open_markets(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        sessions = market_session_status()
        held_markets = {
            str(item.get("market") or "").strip().upper()
            for item in holdings
            if number(item.get("quantity"), 0) > 0
        }
        open_markets = [
            market
            for market in sessions["open_markets"]
            if market in held_markets
        ]
        searchable_holdings = [
            item
            for item in holdings
            if number(item.get("quantity"), 0) > 0
            and (
                str(item.get("market") or "").strip().upper() in open_markets
                or str(item.get("asset_type") or "").strip().upper() == "FUND"
                or str(item.get("market") or "").strip().upper() == "FUND"
            )
        ]
        if not holdings or not searchable_holdings:
            return {
                "ok": True,
                "not_modified": True,
                "state_revision": self._state_revision(state),
                "market_sessions": sessions,
                "market_quote_sync": {
                    "status": "market_closed" if holdings else "no_portfolio",
                    "open_markets": open_markets,
                    "requested_count": 0,
                    "updated_count": 0,
                },
            }
        if self.ai_connections is not None:
            star_result = await asyncio.to_thread(
                self.ai_connections.search_investments_sync,
                searchable_holdings,
            )
            if not star_result.get("results"):
                raise ValueError(
                    str(star_result.get("message") or "星澄尚未連線，報價未送出且不排隊。")
                )
            quotes: list[dict[str, Any]] = []
            bars: list[dict[str, Any]] = []
            for item in star_result.get("results", []):
                if not isinstance(item, dict) or not item.get("trusted"):
                    continue
                parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
                price = number(parameters.get("price"), 0)
                if price <= 0:
                    continue
                source = next(
                    (entry for entry in item.get("sources", []) if isinstance(entry, dict)),
                    {},
                )
                quote = {
                    "symbol": str(item.get("requested_symbol") or "").upper(),
                    "market": str(item.get("market") or "").upper(),
                    "current_price": price,
                    "currency": str(item.get("currency") or "").upper(),
                    "observed_at": str(item.get("observed_at") or ""),
                    "source_url": str(source.get("url") or ""),
                    "resolved_symbol": str(item.get("resolved_symbol") or ""),
                    "quote_kind": str(item.get("quote_kind") or "market_price"),
                }
                quotes.append(quote)
                bars.append(
                    {
                        "symbol": quote["symbol"],
                        "observed_at": quote["observed_at"],
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": None,
                        "currency": quote["currency"],
                        "provider": "star-web-search",
                        "verified": True,
                    }
                )
            prices_added = self.analytics_store.add_price_bars(bars) if bars else 0
            result = {
                "provider": "星澄即時網路搜尋",
                "updated_at": star_result.get("searched_at"),
                "data_as_of": max((str(item.get("observed_at") or "") for item in quotes), default=""),
                "open_markets": open_markets,
                "requested_count": star_result.get("requested_count", len(searchable_holdings)),
                "updated_count": len(quotes),
                "coverage_percent": round(len(quotes) / len(searchable_holdings) * 100, 2) if searchable_holdings else 100.0,
                "prices_added": prices_added,
                "quotes": quotes,
                "error_count": star_result.get("error_count", 0),
                "errors": star_result.get("errors", []),
                "methodology": "星澄搜尋具來源與日期的市價或基金淨值",
                "limitations": ["共同基金淨值不是盤中成交價。", "無法驗證的結果不寫入。"],
            }
        else:
            # Test-only compatibility path. Production constructs this service
            # with authenticated Star connections.
            result = await asyncio.to_thread(
                sync_yahoo_open_market_quotes,
                self.analytics_store,
                holdings,
                open_markets,
            )
        quote_map = {
            (
                str(item.get("market") or "").upper(),
                str(item.get("symbol") or "").upper(),
            ): item
            for item in result.get("quotes", [])
            if isinstance(item, dict)
        }
        if quote_map:
            fx_cache: dict[str, dict[str, Any] | None] = {}
            updated_holdings: list[dict[str, Any]] = []
            for holding in holdings:
                enriched = dict(holding)
                key = (
                    str(holding.get("market") or "").upper(),
                    str(holding.get("symbol") or "").upper(),
                )
                quote = quote_map.get(key)
                if isinstance(quote, dict):
                    current_price = number(quote.get("current_price"), 0)
                    source_currency = str(
                        quote.get("currency") or holding.get("currency") or "TWD"
                    ).upper()
                    enriched["web_current_price"] = current_price
                    enriched["web_current_price_currency"] = source_currency
                    enriched["market_data_source"] = str(
                        result.get("provider") or "星澄即時網路搜尋"
                    )
                    enriched["market_data_source_url"] = str(
                        quote.get("source_url") or ""
                    )
                    enriched["market_data_updated_at"] = str(
                        quote.get("observed_at") or ""
                    )
                    if (
                        str(holding.get("asset_type") or "").upper() == "FUND"
                        and quote.get("resolved_symbol")
                    ):
                        enriched["fund_quote_symbol"] = str(
                            quote.get("resolved_symbol") or ""
                        ).upper()
                        enriched["fund_identity_status"] = "confirmed"
                    if source_currency not in fx_cache:
                        fx_cache[source_currency] = self.v3.fx_rate(
                            source_currency,
                            "TWD",
                        )
                    fx_quote = fx_cache[source_currency]
                    if current_price > 0 and fx_quote and number(fx_quote.get("rate"), 0) > 0:
                        enriched["web_current_value_twd"] = round(
                            current_price
                            * number(holding.get("quantity"), 0)
                            * number(fx_quote.get("rate")),
                            4,
                        )
                    principal_amount = number(
                        holding.get("principal_amount"),
                        number(holding.get("average_cost"), 0)
                        * number(holding.get("quantity"), 0),
                    )
                    principal_currency = str(
                        holding.get("principal_currency")
                        or holding.get("currency")
                        or "TWD"
                    ).upper()
                    if principal_currency not in fx_cache:
                        fx_cache[principal_currency] = self.v3.fx_rate(
                            principal_currency,
                            "TWD",
                        )
                    principal_fx = fx_cache[principal_currency]
                    if principal_amount > 0 and principal_fx and number(principal_fx.get("rate"), 0) > 0:
                        enriched["principal_amount"] = principal_amount
                        enriched["principal_currency"] = principal_currency
                        enriched["principal_twd"] = round(
                            principal_amount * number(principal_fx.get("rate")),
                            4,
                        )
                updated_holdings.append(enriched)
            market_quote_sync = {
                "provider": result.get("provider"),
                "updated_at": result.get("updated_at"),
                "open_markets": open_markets,
                "requested_count": result.get("requested_count", 0),
                "updated_count": result.get("updated_count", 0),
                "error_count": result.get("error_count", 0),
            }
            self._merge_synchronized_holdings(
                holdings,
                updated_holdings,
                sync_metadata={"market_quote_sync": market_quote_sync},
                sync_owned_fields={
                    "web_current_price",
                    "web_current_price_currency",
                    "web_current_value_twd",
                    "market_data_source",
                    "market_data_source_url",
                    "market_data_updated_at",
                    "fund_quote_symbol",
                    "fund_identity_status",
                },
            )
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": (
                    f"星澄報價已更新 "
                    f"{result.get('updated_count', 0)} 筆。"
                ),
                "market_sessions": sessions,
                "market_quote_sync": result,
            }
        )
        return response

    async def _sync_dividends(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [
            dict(item) for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        if not holdings:
            raise ValueError("請先讀取持股檔，再由星澄搜尋配息")
        currencies = [
            str(currency or "")
            for item in holdings
            for currency in (
                item.get("currency"),
                item.get("principal_currency"),
            )
        ]
        fx_result = await asyncio.to_thread(
            sync_fx_from_huanan_bank,
            self.v3,
            currencies,
            "TWD",
        )
        if self.ai_connections is not None:
            star_result = await asyncio.to_thread(
                self.ai_connections.search_investments_sync,
                [item for item in holdings if number(item.get("quantity"), 0) > 0],
            )
            if not star_result.get("results"):
                raise ValueError(
                    str(star_result.get("message") or "星澄尚未連線，配息搜尋未送出且不排隊。")
                )
            star_updates: list[dict[str, Any]] = []
            for item in star_result.get("results", []):
                if not isinstance(item, dict) or not item.get("trusted"):
                    continue
                parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
                distribution = item.get("distribution") if isinstance(item.get("distribution"), dict) else {}
                source = next(
                    (entry for entry in item.get("sources", []) if isinstance(entry, dict)),
                    {},
                )
                market = str(item.get("market") or "").upper()
                requested_symbol = str(item.get("requested_symbol") or "").upper()
                events = [
                    {
                        "occurred_at": event.get("observed_at") or event.get("record_date"),
                        "amount_per_unit": event.get("amount_per_unit"),
                        "source_url": event.get("source_url"),
                        "title": event.get("title"),
                    }
                    for event in item.get("distribution_events", [])
                    if isinstance(event, dict)
                ]
                trailing = number(parameters.get("annual_distribution_per_unit"), 0)
                star_updates.append(
                    {
                        "symbol": requested_symbol,
                        "market": market,
                        "requested_symbol": str(item.get("resolved_symbol") or requested_symbol),
                        "currency": str(item.get("currency") or "").upper(),
                        "name": str(item.get("name") or ""),
                        "instrument_type": "MUTUALFUND" if item.get("asset_type") == "FUND" else str(item.get("asset_type") or "").upper(),
                        "exchange_name": "基金資訊觀測站" if item.get("asset_type") == "FUND" else "",
                        "current_price": number(parameters.get("price"), 0),
                        "trailing_annual_dividend_per_unit": trailing,
                        "annual_dividend_yield_percent": parameters.get("distribution_yield_percent"),
                        "event_count": len(events),
                        "events": events,
                        "dividend_frequency": str(distribution.get("frequency") or "unknown"),
                        "dividend_frequency_label": str(distribution.get("frequency_label") or "待累積資料"),
                        "dividend_frequency_per_year": distribution.get("frequency_per_year"),
                        "dividend_frequency_confidence": distribution.get("frequency_confidence"),
                        "source": "星澄即時網路搜尋",
                        "source_url": str(source.get("url") or ""),
                        "updated_at": str(item.get("observed_at") or star_result.get("searched_at") or ""),
                        "status": "updated" if trailing > 0 else "no_distribution" if distribution.get("frequency") == "none" else "distribution_evidence_only" if events or distribution.get("frequency") not in {None, "", "unknown"} else "no_external_dividend",
                        "official_code": str(item.get("official_code") or ""),
                    }
                )
            dividend_result = {
                "requested_count": star_result.get("requested_count", 0),
                "updated_count": sum(item.get("status") in {"updated", "distribution_evidence_only", "no_distribution"} for item in star_updates),
                "no_distribution_count": sum(item.get("status") == "no_distribution" for item in star_updates),
                "no_dividend_count": sum(item.get("status") in {"no_distribution", "no_external_dividend"} for item in star_updates),
                "error_count": star_result.get("error_count", 0),
                "updates": star_updates,
                "errors": star_result.get("errors", []),
                "provider": "星澄即時網路搜尋",
                "updated_at": star_result.get("searched_at"),
                "coverage_percent": round(len(star_updates) / max(1, int(star_result.get("requested_count") or 0)) * 100, 2),
                "methodology": "星澄搜尋公開市場配息事件與官方基金配息公告",
                "limitations": ["基金公告未揭露可驗證金額時只保存公告與頻率，不推造配息金額。"],
            }
        else:
            dividend_result = await asyncio.to_thread(
                sync_yahoo_dividends,
                holdings,
                period="2y",
            )
        updates = {
            (str(item.get("market") or ""), str(item.get("symbol") or "")): item
            for item in dividend_result.get("updates", [])
            if isinstance(item, dict)
        }
        updated_holdings: list[dict[str, Any]] = []
        event_payloads: list[dict[str, Any]] = []
        for holding in holdings:
            enriched = self._enrich_holding_principal_basis(holding)
            key = (
                str(holding.get("market") or "").upper(),
                str(holding.get("symbol") or "").upper(),
            )
            update = updates.get(key)
            if not isinstance(update, dict):
                updated_holdings.append(enriched)
                continue
            enriched["dividend_source"] = str(update.get("source") or "星澄即時網路搜尋")
            enriched["dividend_source_url"] = str(update.get("source_url") or "")
            enriched["dividend_updated_at"] = str(update.get("updated_at") or "")
            enriched["dividend_status"] = str(update.get("status") or "")
            current_name = str(enriched.get("name") or "").strip()
            online_name = str(update.get("name") or "").strip()
            if online_name and (
                not current_name or current_name.upper() == key[1].upper()
            ):
                enriched["name"] = online_name
                enriched["name_source"] = str(update.get("source") or "星澄即時網路搜尋")
            instrument_type = str(update.get("instrument_type") or "").upper()
            asset_type_map = {
                "EQUITY": "STOCK",
                "ETF": "ETF",
                "MUTUALFUND": "FUND",
            }
            if str(enriched.get("asset_type") or "").upper() in {"", "AUTO"}:
                enriched["asset_type"] = asset_type_map.get(
                    instrument_type,
                    enriched.get("asset_type") or "AUTO",
                )
            online_currency = str(update.get("currency") or "").upper()
            if online_currency and not str(enriched.get("currency") or "").strip():
                enriched["currency"] = online_currency
            enriched["market_data_source"] = str(update.get("source") or "星澄即時網路搜尋")
            if update.get("official_code"):
                enriched["fund_quote_symbol"] = str(update["official_code"])
                enriched["fund_identity_status"] = "confirmed"
            enriched["market_data_source_url"] = str(update.get("source_url") or "")
            enriched["market_data_updated_at"] = str(update.get("updated_at") or "")
            enriched["exchange_name"] = str(update.get("exchange_name") or "")
            enriched["dividend_frequency"] = str(
                update.get("dividend_frequency") or "unknown"
            )
            enriched["dividend_frequency_label"] = str(
                update.get("dividend_frequency_label") or "待累積資料"
            )
            enriched["dividend_frequency_per_year"] = update.get(
                "dividend_frequency_per_year"
            )
            enriched["dividend_frequency_median_days"] = update.get(
                "dividend_frequency_median_days"
            )
            enriched["dividend_frequency_confidence"] = update.get(
                "dividend_frequency_confidence"
            )
            enriched["external_annual_dividend_per_unit"] = update.get(
                "trailing_annual_dividend_per_unit"
            )
            if update.get("annual_dividend_yield_percent") is not None:
                enriched["annual_dividend_yield_percent"] = update[
                    "annual_dividend_yield_percent"
                ]
            annual_per_unit = number(
                update.get("trailing_annual_dividend_per_unit"), 0
            )
            annual_native = annual_per_unit * number(holding.get("quantity"), 0)
            source_currency = str(
                update.get("currency") or holding.get("currency") or "TWD"
            ).upper()
            fx_quote = self.v3.fx_rate(source_currency, "TWD")
            current_price = number(update.get("current_price"), 0)
            if current_price > 0:
                enriched["web_current_price"] = current_price
                enriched["web_current_price_currency"] = source_currency
                if fx_quote and number(fx_quote.get("rate"), 0) > 0:
                    enriched["web_current_value_twd"] = round(
                        current_price
                        * number(holding.get("quantity"), 0)
                        * number(fx_quote.get("rate")),
                        4,
                    )
            if annual_native > 0 and fx_quote and number(fx_quote.get("rate"), 0) > 0:
                annual_twd = annual_native * number(fx_quote["rate"])
                enriched["estimated_annual_dividend_twd"] = round(annual_twd, 4)
                enriched["estimated_weekly_dividend_twd"] = round(
                    annual_twd / 52.0, 4
                )
                enriched["monthly_dividend_twd"] = round(annual_twd / 12.0, 4)
                enriched["dividend_fx_provider"] = str(
                    fx_quote.get("provider") or ""
                )
                enriched["dividend_fx_rate"] = number(fx_quote.get("rate"))
            for event in update.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_payloads.append(
                    {
                        "event_type": "dividend",
                        "symbol": key[1],
                        "title": f"{key[1]} 星澄配息搜尋",
                        "scheduled_at": event.get("occurred_at"),
                        "source": update.get("source") or "Yahoo Finance",
                        "source_url": event.get("source_url") or update.get("source_url") or "",
                        "confidence": 0.85,
                        "details": {
                            "amount": event.get("amount_per_unit"),
                            "currency": source_currency,
                        },
                        "dedupe_key": (
                            f"online-dividend|{key[0]}|{key[1]}|"
                            f"{event.get('occurred_at')}"
                        ),
                    }
                )
            updated_holdings.append(enriched)
        if event_payloads:
            await asyncio.to_thread(
                self.analytics_store.add_events,
                event_payloads,
            )
        event_count = len(event_payloads)
        dividend_sync = {
            "provider": dividend_result.get("provider"),
            "updated_at": local_device_now().isoformat(),
            "portfolio_imported_at": str(
                (state.get("portfolio") or {}).get("imported_at") or ""
            ),
            "portfolio_manual_revision": int(
                (state.get("portfolio") or {}).get("manual_revision") or 0
            ),
            "requested_count": dividend_result.get("requested_count", 0),
            "updated_count": dividend_result.get("updated_count", 0),
            "error_count": dividend_result.get("error_count", 0),
            "fx_provider": fx_result.get("fx_provider"),
            "fx_observed_at": fx_result.get("fx_observed_at"),
            "weekly_standard": True,
            "display_currency": "TWD",
        }
        await asyncio.to_thread(
            self._merge_synchronized_holdings,
            holdings,
            updated_holdings,
            sync_metadata={"dividend_sync": dividend_sync},
            sync_owned_fields={
                "dividend_source",
                "dividend_source_url",
                "dividend_updated_at",
                "dividend_status",
                "dividend_frequency",
                "dividend_frequency_label",
                "dividend_frequency_per_year",
                "dividend_frequency_median_days",
                "dividend_frequency_confidence",
                "external_annual_dividend_per_unit",
                "annual_dividend_yield_percent",
                "estimated_annual_dividend_twd",
                "estimated_weekly_dividend_twd",
                "monthly_dividend_twd",
                "dividend_fx_provider",
                "dividend_fx_rate",
                "web_current_price",
                "web_current_price_currency",
                "web_current_value_twd",
                "market_data_source",
                "market_data_source_url",
                "market_data_updated_at",
                "exchange_name",
                "fund_quote_symbol",
                "fund_identity_status",
            },
        )
        await asyncio.to_thread(
            self.analytics_store.audit,
            "online_dividend_sync",
            {
                **dividend_sync,
                "event_count": event_count,
                "errors": dividend_result.get("errors", [])[:20],
            },
            severity=(
                "warning" if number(dividend_result.get("error_count")) > 0 else "info"
            ),
        )
        self._invalidate_v3_snapshot()
        latest_state = await asyncio.to_thread(self.repository.load_state)
        response = await asyncio.to_thread(self._state_response, latest_state)
        response.update(
            {
                "message": (
                    f"星澄配息搜尋已更新 {dividend_result.get('updated_count', 0)} 筆；"
                    "週配息已依華南銀行匯率換算為新台幣。"
                ),
                "dividend_sync": dividend_result,
                "fx_sync": fx_result,
            }
        )
        return response

    async def _run_stress_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenarios = payload.get("scenarios")
        if scenarios is not None and not isinstance(scenarios, list):
            raise ValueError("scenarios 必須是情境陣列")
        state = self.repository.load_state()
        result = self.analytics_store.stress_test(state, scenarios)
        response = self._state_response(state)
        response.update({"message": "壓力測試完成。", "stress_test": result})
        return response

    async def _run_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        raw_symbols = payload.get("symbols")
        symbols = (
            [str(item).strip().upper() for item in raw_symbols if str(item).strip()]
            if isinstance(raw_symbols, list)
            else [str(item.get("symbol") or "").upper() for item in state.get("holdings", []) if isinstance(item, dict)]
        )
        result = self.analytics_store.backtest(
            symbols,
            strategy=str(payload.get("strategy") or "buy_and_hold"),
            initial_capital=float(payload.get("initial_capital") or 1_000_000),
            fee_percent=float(payload.get("fee_percent") or 0.1425),
            slippage_percent=float(payload.get("slippage_percent") or 0.05),
        )
        response = self._state_response(state)
        response.update({"message": "歷史回測完成。", "backtest": result})
        return response

    async def _plan_rebalance(self, payload: dict[str, Any]) -> dict[str, Any]:
        targets = payload.get("targets")
        if targets is not None and not isinstance(targets, dict):
            raise ValueError("targets 必須是標的與目標權重物件")
        state = self.repository.load_state()
        policy = self._investment_policy()
        forbidden = {
            str(item).strip().upper()
            for item in (policy.get("forbidden_assets") or [])
            if str(item).strip()
        }
        normalized_targets = (
            {str(key).upper(): float(value) for key, value in targets.items()}
            if isinstance(targets, dict)
            else None
        )
        if normalized_targets is not None and forbidden:
            holding_types = {
                str(item.get("symbol") or "").upper(): str(
                    item.get("asset_type") or ""
                ).upper()
                for item in state.get("holdings", [])
                if isinstance(item, dict)
            }
            for symbol in list(normalized_targets):
                if symbol in forbidden or holding_types.get(symbol) in forbidden:
                    normalized_targets[symbol] = 0.0
        capacity_caps = {
            "conservative": 15.0,
            "balanced": 25.0,
            "growth": 35.0,
            "aggressive": 50.0,
        }
        risk_capacity = str(policy.get("risk_capacity") or "balanced")
        policy_cap = capacity_caps.get(risk_capacity, 25.0)
        max_position_percent = min(
            float(payload.get("max_position_percent") or 35),
            policy_cap,
        )
        cash_reserve_percent = max(
            float(payload.get("cash_reserve_percent") or 5),
            number(policy.get("cash_need_percent"), 0),
        )
        result = self.analytics_store.rebalance(
            state,
            targets=normalized_targets,
            max_position_percent=max_position_percent,
            cash_reserve_percent=cash_reserve_percent,
            min_trade_value=float(payload.get("min_trade_value") or 1000),
            fee_percent=float(payload.get("fee_percent") or 0.1425),
        )
        result["policy_constraints"] = {
            "risk_capacity": risk_capacity,
            "max_position_percent": max_position_percent,
            "cash_reserve_percent": cash_reserve_percent,
            "forbidden_assets": sorted(forbidden),
            "human_approval_required": True,
        }
        response = self._state_response(state)
        response.update({"message": "再平衡模擬完成，尚未送出任何交易。", "rebalance": result})
        return response

    async def _add_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.analytics_store.add_event(payload)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "市場事件已加入行事曆。", "event": event})
        return response

    async def _add_alert_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule = self.analytics_store.add_alert_rule(payload)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "持久警示規則已儲存。", "alert_rule": rule})
        return response

    async def _acknowledge_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        alert_event_id = str(payload.get("alert_event_id") or "").strip()
        if not alert_event_id:
            raise ValueError("缺少 alert_event_id")
        acknowledged = self.analytics_store.acknowledge_alert(alert_event_id)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "警示已確認。" if acknowledged else "找不到警示。", "acknowledged": acknowledged})
        return response

    async def _set_decision_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision_id = str(payload.get("decision_id") or "").strip()
        if not decision_id:
            raise ValueError("缺少 decision_id")
        updated = self.analytics_store.set_decision_status(
            decision_id,
            str(payload.get("status") or "pending"),
        )
        response = self._state_response(self.repository.load_state())
        response.update({"message": "決策狀態已更新。" if updated else "找不到決策。", "updated": updated})
        return response

    async def _update_v2_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "base_currency",
            "benchmark",
            "max_position_percent",
            "cash_reserve_percent",
            "mobile_pairing_ttl_hours",
            "investment_goal",
            "time_horizon_years",
            "risk_capacity",
            "cash_need_percent",
            "forbidden_assets",
            "target_return_percent",
        }
        saved: dict[str, Any] = {}
        for key in allowed:
            if key in payload:
                value = payload[key]
                if key == "risk_capacity":
                    normalized = str(value or "").strip().casefold()
                    if normalized not in {
                        "conservative",
                        "balanced",
                        "growth",
                        "aggressive",
                    }:
                        raise ValueError("risk_capacity 不在允許範圍")
                    value = normalized
                elif key == "investment_goal":
                    value = str(value or "").strip()[:500]
                elif key == "forbidden_assets":
                    values = value if isinstance(value, list) else str(value or "").split(",")
                    value = sorted(
                        {
                            str(item).strip().upper()
                            for item in values
                            if str(item).strip()
                        }
                    )[:100]
                elif key == "time_horizon_years":
                    value = max(1.0, min(80.0, number(value, 5)))
                elif key in {"cash_need_percent", "cash_reserve_percent"}:
                    value = max(0.0, min(100.0, number(value, 0)))
                elif key == "max_position_percent":
                    value = max(1.0, min(100.0, number(value, 35)))
                elif key == "target_return_percent":
                    value = (
                        None
                        if value in {None, ""}
                        else max(-100.0, min(1000.0, number(value)))
                    )
                self.analytics_store.set_setting(key, value)
                saved[key] = value
        if "mobile_pairing_ttl_hours" in saved:
            self.mobile_sync_gateway.set_pairing_ttl_hours(float(saved["mobile_pairing_ttl_hours"]))
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update({"message": "投資管家設定已更新。", "settings": saved})
        return response

    def _invalidate_v3_snapshot(self) -> None:
        self._v3_snapshot_cache = None
        self._v3_snapshot_cached_at = 0.0

    async def _get_v3(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        return {"ok": True, "version": self.VERSION, "v3": self._v3_snapshot(state, force=True)}

    async def _add_fx_rates(self, payload: dict[str, Any]) -> dict[str, Any]:
        rates = payload.get("rates")
        if not isinstance(rates, list):
            rates = [payload]
        count = self.v3.add_fx_rates([item for item in rates if isinstance(item, dict)])
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update({"message": f"已寫入 {count} 筆歷史匯率。", "fx_rate_count": count})
        return response

    async def _import_broker_statement(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(payload.get("path") or payload.get("file_path") or "").strip()
        if not raw_path:
            raise ValueError("請選擇券商 CSV、Excel 或 PDF 明細")
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"找不到檔案：{source}")
        if source.suffix.casefold() not in {".csv", ".tsv", ".txt", ".xlsx", ".pdf"}:
            raise ValueError("券商明細僅支援 CSV、TSV、XLSX 或文字型 PDF")
        snapshot = self._create_import_snapshot(source)
        try:
            result = self.broker_reconciliation.import_statement(
                snapshot,
                source_name=source.name,
                broker=str(payload.get("broker") or ""),
            )
        finally:
            # Complete source snapshots are append-only recovery evidence.
            pass
        self._invalidate_v3_snapshot()
        return {
            "ok": True,
            "message": f"已比對 {result['row_count']} 筆，發現 {result['difference_count']} 筆差異；尚未自動入帳。",
            "broker_import": result,
            "source_file_released": True,
            "v3": self._v3_snapshot(self.repository.load_state(), force=True),
            "state": self._state_with_analytics(self.repository.load_state()),
        }

    async def _approve_broker_rows(self, payload: dict[str, Any]) -> dict[str, Any]:
        row_ids = payload.get("row_ids")
        if not isinstance(row_ids, list):
            raise ValueError("row_ids 必須是待入帳差異清單")
        result = self.broker_reconciliation.approve_rows(
            str(payload.get("import_id") or ""), row_ids, confirmed=payload.get("confirmed") is True
        )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "已將確認的差異寫入本機交易帳本。", "broker_import": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _optimize_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        result = self.v3.optimize_portfolio(
            state,
            method=str(payload.get("method") or "risk_parity"),
            max_position_percent=float(payload.get("max_position_percent") or 35),
            max_turnover_percent=float(payload.get("max_turnover_percent") or 40),
            views=payload.get("views") if isinstance(payload.get("views"), dict) else None,
            seed=int(payload.get("seed") or 73021),
        )
        return {"ok": bool(result.get("ok")), "message": "最佳化草案已完成，沒有送出任何交易。", "optimization": result}

    async def _run_monte_carlo(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.v3.monte_carlo(
            self.repository.load_state(),
            simulations=int(payload.get("simulations") or 2000),
            horizon_days=int(payload.get("horizon_days") or 252),
            target_return_percent=float(payload.get("target_return_percent") or 0),
            seed=int(payload.get("seed") or 73021),
        )
        return {"ok": bool(result.get("ok")), "message": "蒙地卡羅情境模擬完成。", "monte_carlo": result}

    async def _add_corporate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = self.v3.add_corporate_action(payload)
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "公司行動已進入人工覆核，不會直接改動持股。", "corporate_action": action, "state": self._state_with_analytics(self.repository.load_state())}

    async def _review_corporate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        updated = self.v3.review_corporate_action(
            str(payload.get("action_id") or ""), str(payload.get("status") or "pending_review")
        )
        self._invalidate_v3_snapshot()
        return {"ok": updated, "message": "公司行動覆核狀態已更新。" if updated else "找不到公司行動。", "state": self._state_with_analytics(self.repository.load_state())}

    async def _configure_scheduler(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self.automation.configure(int(payload.get("interval_seconds") or 900))
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "背景排程週期已更新。", "automation": status, "state": self._state_with_analytics(self.repository.load_state())}

    async def _run_scheduler(self, _payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.automation.run_once()
        self._invalidate_v3_snapshot()
        return {"ok": result["status"] == "completed", "message": "背景監測週期已完成。", "scheduler_run": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _configure_notification(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = self.notifications.configure(
            str(payload.get("channel_id") or ""), payload, confirmed=payload.get("confirmed") is True
        )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "通知管道已更新。", "channel": channel, "state": self._state_with_analytics(self.repository.load_state())}

    async def _dispatch_notifications(self, _payload: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.to_thread(self.notifications.dispatch)
        self._invalidate_v3_snapshot()
        return {"ok": result["failed"] == 0, "message": "通知佇列已處理。", "dispatch": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _record_model_evaluation(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = self.model_governance.record_run(
            model_name=str(payload.get("model_name") or "local-risk-ai"),
            version=str(payload.get("version") or self.VERSION),
            inputs=payload.get("inputs") or {},
            outputs=payload.get("outputs") or {},
            data_sources=[str(value) for value in payload.get("data_sources", [])] if isinstance(payload.get("data_sources"), list) else [],
            metrics=payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
            prompt=str(payload.get("prompt") or ""),
            analysis_run_id=str(payload.get("analysis_run_id") or ""),
            decision_count=int(payload.get("decision_count") or 0),
        )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "模型評估紀錄已寫入治理帳本。", "governance_run_id": run_id, "state": self._state_with_analytics(self.repository.load_state())}

    async def _create_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator():
            backup = self.analytics_store.backup_database(
                str(payload.get("label") or "manual")
            )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "加密備份已建立。", "backup": backup, "state": self._state_with_analytics(self.repository.load_state())}

    async def _restore_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("還原資料庫前必須明確確認")
        with self._snapshot_coordinator():
            result = self.analytics_store.restore_database(
                str(payload.get("backup_name") or "")
            )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "資料庫已還原，並保留還原前安全備份。", "restore": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _rotate_database_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("更換資料庫保護金鑰前必須明確確認")
        with self._snapshot_coordinator():
            result = self.analytics_store.rotate_database_protection()
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "資料庫保護金鑰已輪替。", "rotation": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _run_automation_cycle(self) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        sync_result: dict[str, Any] = {"status": "no_portfolio"}
        if holdings:
            sync_result = await asyncio.to_thread(
                sync_yahoo_intelligence, self.analytics_store, holdings, period="1y"
            )
            fx_result = await asyncio.to_thread(
                sync_fx_from_huanan_bank,
                self.v3,
                [str(item.get("currency") or "") for item in holdings],
                str(self.analytics_store.get_setting("base_currency", "TWD") or "TWD"),
            )
            sync_result.update(fx_result)
            factor_result = await asyncio.to_thread(
                sync_factor_proxies_from_yahoo,
                self.v3,
                period="2y",
            )
            sync_result.update(factor_result)
            sync_result["corporate_actions_added"] = self._ingest_corporate_actions_from_events()
        alerts = self.analytics_store.evaluate_alerts(state)
        for alert in alerts:
            self.notifications.queue(
                str(alert.get("title") or "投資風險事件"),
                str(alert.get("detail") or ""),
                severity=str(alert.get("severity") or "warning"),
            )
        self._invalidate_v3_snapshot()
        return {"sync": sync_result, "alerts_triggered": len(alerts)}

    def _ingest_corporate_actions_from_events(self) -> int:
        added = 0
        for event in self.analytics_store.list_events(500):
            event_type = str(event.get("event_type") or "")
            if event_type not in {"dividend", "split"}:
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            split_ratio = None
            if event_type == "split":
                numerator = float(details.get("numerator") or 0)
                denominator = float(details.get("denominator") or 0)
                split_ratio = numerator / denominator if numerator > 0 and denominator > 0 else None
            action = self.v3.add_corporate_action(
                {
                    "symbol": event.get("symbol"),
                    "action_type": event_type,
                    "effective_at": event.get("scheduled_at"),
                    "cash_amount": details.get("amount") if event_type == "dividend" else None,
                    "currency": details.get("currency") or "",
                    "ratio": split_ratio,
                    "source": event.get("source") or "Yahoo Finance",
                    "confidence": event.get("confidence") or 0.8,
                    "details": details,
                }
            )
            added += int(bool(action.get("created")))
        return added

    def _create_import_snapshot(self, source: Path) -> Path:
        try:
            return investment_manager_core.create_portfolio_file_snapshot(
                source,
                self.repository.runtime_root / "imports",
                keep=self.IMPORT_SNAPSHOT_KEEP,
            )
        except investment_manager_core.InvestmentManagerError as exc:
            raise investment_manager_core.InvestmentManagerError(
                f"無法建立匯入快照，原始檔不會被長時間鎖定。請確認檔案可讀取後再試：{exc}"
            ) from exc

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _portfolio_import_fingerprint(
        source_digest: str,
        parameters: dict[str, Any],
    ) -> str:
        normalized = json.dumps(
            parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(
            f"{source_digest}|{normalized}".encode("utf-8")
        ).hexdigest()

    def _prepare_import_operation(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        source = self._excel_mapping_source(payload)
        source_stat = source.stat()
        source_digest = self._file_digest(source)
        durable_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"operation_id", "poll", "poll_only"}
        }
        durable_payload["path"] = str(source)
        durable_payload["_source_sha256"] = source_digest
        request_material = {
            "source_path": str(source),
            "source_size": int(source_stat.st_size),
            "source_mtime_ns": int(source_stat.st_mtime_ns),
            "source_sha256": source_digest,
            "layout": durable_payload.get("layout"),
            "config": durable_payload.get("config"),
            "sheets": durable_payload.get("sheets"),
            "sheet_name": durable_payload.get("sheet_name"),
            "header_row_number": durable_payload.get("header_row_number"),
            "data_start_row_number": durable_payload.get(
                "data_start_row_number"
            ),
            "column_mapping": durable_payload.get("column_mapping"),
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(
                request_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return self.analytics_store.create_or_resume_import_operation(
            request_fingerprint,
            durable_payload,
        )

    def _resume_import_operations(self) -> None:
        for operation in self.analytics_store.resumable_import_operations():
            operation_id = str(operation.get("operation_id") or "")
            if not operation_id:
                continue
            if operation.get("status") != "queued":
                operation = self.analytics_store.update_import_operation(
                    operation_id,
                    status="queued",
                    reason="service_restart_recovery",
                )
            self._ensure_import_operation_task(operation)

    def _ensure_import_operation_task(
        self,
        operation: dict[str, Any],
    ) -> asyncio.Task[dict[str, Any]] | None:
        operation_id = str(operation.get("operation_id") or "")
        if not operation_id or operation.get("status") in {"completed", "failed"}:
            return None
        current = self._import_operation_tasks.get(operation_id)
        if current is not None and not current.done():
            return current
        if self._shutdown_started:
            return None
        task = asyncio.create_task(
            self._await_daemon_worker(
                self._execute_import_operation,
                operation_id,
                worker_name=f"investment-import-{operation_id[:12]}",
            ),
            name=f"investment-import-{operation_id[:12]}",
        )
        self._import_operation_tasks[operation_id] = task
        task.add_done_callback(
            lambda completed, oid=operation_id: self._import_operation_finished(
                oid,
                completed,
            )
        )
        return task

    def _import_operation_finished(
        self,
        operation_id: str,
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        if self._import_operation_tasks.get(operation_id) is task:
            self._import_operation_tasks.pop(operation_id, None)
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        self._maybe_close_storage()

    def _durable_worker_finished(
        self,
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        self._durable_worker_tasks.discard(task)
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        self._maybe_close_storage()

    def _background_job_finished(self, task: asyncio.Task[Any]) -> None:
        self._background_jobs.discard(task)
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        self._maybe_close_storage()

    def _execute_import_operation(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        operation = self.analytics_store.get_import_operation(operation_id)
        if operation is None:
            raise ValueError("import operation not found")
        if operation.get("status") == "completed":
            return operation
        if self._import_shutdown_event.is_set():
            return self.analytics_store.update_import_operation(
                operation_id,
                status="resume_pending",
                reason="shutdown_before_worker_start",
            )
        operation = self.analytics_store.update_import_operation(
            operation_id,
            status="processing",
            reason="worker_claimed",
            increment_attempt=True,
        )
        worker_payload = dict(operation.get("payload") or {})
        worker_payload["refresh_quotes"] = False
        worker_payload["_operation_id"] = operation_id
        try:
            response = self._import_excel_mapping_sync(worker_payload)
        except _ImportResumePending:
            return self.analytics_store.update_import_operation(
                operation_id,
                status="resume_pending",
                reason="shutdown_before_commit",
            )
        except Exception as exc:
            return self.analytics_store.update_import_operation(
                operation_id,
                status="failed",
                reason="worker_failed",
                error=InvestmentWatchRepository._shorten(str(exc), 2000),
            )
        summary_keys = {
            "message",
            "import_mode",
            "deduplicated",
            "source_file_released",
            "source_file_modified",
            "excel_import_profile",
            "imported_row_count",
            "skipped_row_count",
            "import_fingerprint",
        }
        summary = {
            key: value
            for key, value in response.items()
            if key in summary_keys
        }
        summary["refresh_quotes_requested"] = bool(
            (operation.get("payload") or {}).get("refresh_quotes", True)
        )
        summary["postprocess_queued"] = False
        return self.analytics_store.update_import_operation(
            operation_id,
            status="completed",
            reason="idempotent_commit_complete",
            import_fingerprint=str(response.get("import_fingerprint") or ""),
            result=summary,
            error="",
        )

    async def _import_operation_response(
        self,
        operation: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = str(operation.get("operation_id") or "")
        status = str(operation.get("status") or "queued")
        common = {
            "operation_id": operation_id,
            "operation_status": status,
            "processing": status in {"queued", "processing", "resume_pending"},
            "fingerprint": str(operation.get("request_fingerprint") or ""),
            "import_fingerprint": str(operation.get("import_fingerprint") or ""),
            "attempt_count": int(operation.get("attempt_count") or 0),
            "poll_after_ms": 750,
            "resumable": True,
        }
        if status == "failed":
            return {
                "ok": False,
                "message": str(operation.get("error") or "Excel 匯入失敗。"),
                **common,
            }
        if status != "completed":
            return {
                "ok": True,
                "message": "Excel 匯入正在後端處理，可用 operation_id 查詢進度。",
                **common,
            }

        summary = dict(operation.get("result") or {})
        state = await asyncio.to_thread(self.repository.load_state)
        if (
            summary.get("refresh_quotes_requested")
            and not summary.get("postprocess_queued")
            and state.get("holdings")
        ):
            trigger_by_mode = {
                "snapshot_consolidated_report": "excel_consolidated_report_import",
                "snapshot_horizontal_matrix": "excel_horizontal_matrix_import",
                "snapshot_manual_mapping": "excel_column_mapping_import",
            }
            local_risk_result = self._schedule_local_risk_ai_background(
                state,
                {
                    "trigger": trigger_by_mode.get(
                        str(summary.get("import_mode") or ""),
                        "excel_mapping_import",
                    ),
                    "live_quotes": True,
                },
            )
            summary["postprocess_queued"] = True
            operation = self.analytics_store.update_import_operation(
                operation_id,
                status="completed",
                reason="postprocess_queued",
                result=summary,
            )
            if isinstance(local_risk_result.get("state"), dict):
                state = local_risk_result["state"]
            summary["local_risk_ai"] = local_risk_result
            summary["product_status"] = local_risk_result.get("product_status")
        response = await asyncio.to_thread(self._state_response, state)
        summary.pop("refresh_quotes_requested", None)
        summary.pop("postprocess_queued", None)
        response.update(summary)
        response.update(common)
        response["operation_status"] = "completed"
        response["processing"] = False
        return response

    async def _run_durable_worker(
        self,
        worker: Any,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Let state-writing work finish without making cancellation wait for it."""

        task = asyncio.create_task(
            self._await_daemon_worker(
                worker,
                payload,
                worker_name="durable-investment-worker",
            )
        )
        self._durable_worker_tasks.add(task)
        task.add_done_callback(self._durable_worker_finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise

    def _excel_mapping_source(self, payload: dict[str, Any]) -> Path:
        state = self.repository.load_state()
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        raw_path = str(
            payload.get("path")
            or payload.get("file_path")
            or payload.get("source_path")
            or portfolio.get("source_path")
            or ""
        ).strip()
        if not raw_path:
            raise ValueError("請先選擇要設定欄位的 Excel 檔案。")
        source = Path(raw_path).expanduser().resolve()
        if not source.exists() or not source.is_file():
            raise ValueError(f"找不到 Excel 檔案：{source}")
        if source.suffix.casefold() not in investment_manager_core.XLSX_EXTENSIONS:
            raise ValueError("欄位設定目前支援 .xlsx；舊版 .xls 請先另存為 .xlsx。")
        return source

    async def _preview_excel_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._preview_excel_mapping_sync,
            dict(payload),
        )

    def _preview_excel_mapping_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = self._excel_mapping_source(payload)
        import_snapshot = self._create_import_snapshot(source)
        try:
            preview = investment_manager_core.xlsx_mapping_preview(import_snapshot)
        finally:
            # Keep the verified source snapshot for audit/recovery.
            pass
        preview.update({"source_path": str(source), "file_name": source.name})
        preview["smart_repair"] = build_smart_mapping_repair(preview)
        horizontal = (
            preview.get("horizontal_layout")
            if isinstance(preview.get("horizontal_layout"), dict)
            else {}
        )
        if horizontal.get("detected"):
            message = (
                f"已辨識 {horizontal.get('sheet_count', 0)} 張橫向持股表，"
                f"預選 ETF、台股、美股與共同基金共 {horizontal.get('holding_count', 0)} 筆。"
            )
        else:
            message = f"已讀取 {preview.get('sheet_count', 0)} 張工作表，可調整欄位後再匯入。"
        return {
            "ok": True,
            "message": message,
            "excel_mapping_preview": preview,
            "source_file_released": True,
            "source_file_modified": False,
        }

    async def _import_excel_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(payload.get("operation_id") or "").strip()
        if operation_id:
            operation = await asyncio.to_thread(
                self.analytics_store.get_import_operation,
                operation_id,
            )
            if operation is None:
                raise ValueError("找不到 Excel 匯入 operation_id。")
            self._ensure_import_operation_task(operation)
            return await self._import_operation_response(operation)

        operation = await asyncio.to_thread(
            self._prepare_import_operation,
            dict(payload),
        )
        reused_completed = operation.get("status") == "completed"
        task = self._ensure_import_operation_task(operation)
        if task is not None:
            done, _pending = await asyncio.wait(
                {task},
                timeout=max(0.05, float(self.IMPORT_INLINE_WAIT_SECONDS)),
            )
            if done:
                operation = task.result()
            else:
                operation = await asyncio.to_thread(
                    self.analytics_store.get_import_operation,
                    str(operation.get("operation_id") or ""),
                )
                if operation is None:
                    raise RuntimeError("Excel 匯入工作狀態遺失。")
        if reused_completed:
            operation = dict(operation)
            operation["result"] = {
                **dict(operation.get("result") or {}),
                "deduplicated": True,
            }
        return await self._import_operation_response(operation)

    def _import_excel_mapping_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = self._excel_mapping_source(payload)
        layout = str(payload.get("layout") or "row_mapping").strip().casefold()
        import_snapshot = self._create_import_snapshot(source)
        try:
            snapshot_digest = self._file_digest(import_snapshot)
            expected_digest = str(
                payload.get("_source_sha256") or ""
            ).strip().lower()
            if expected_digest and snapshot_digest != expected_digest:
                raise investment_manager_core.InvestmentManagerError(
                    "Excel 來源檔在排程後已變更；本次未寫入，請重新送出匯入。"
                )
            import_fingerprint = self._portfolio_import_fingerprint(
                snapshot_digest,
                {
                    "source_path": str(source),
                    "layout": layout,
                    "config": payload.get("config"),
                    "sheets": payload.get("sheets"),
                    "sheet_name": payload.get("sheet_name"),
                    "header_row_number": payload.get("header_row_number"),
                    "data_start_row_number": payload.get(
                        "data_start_row_number"
                    ),
                    "column_mapping": payload.get("column_mapping"),
                },
            )
            if layout == "consolidated_report":
                raw_config = payload.get("config")
                if not isinstance(raw_config, dict):
                    raise ValueError("請完成報酬工作表的欄位設定。")
                holdings, details = (
                    investment_manager_core.load_xlsx_portfolio_consolidated_report(
                        import_snapshot,
                        config=raw_config,
                    )
                )
                sheet_name = str(raw_config.get("sheet_name") or "報酬").strip()
                audit_event = "excel_consolidated_report_import"
                import_mode = "snapshot_consolidated_report"
            elif layout == "horizontal_matrix":
                sheet_configs = payload.get("sheets")
                if not isinstance(sheet_configs, list):
                    raise ValueError("請選擇至少一張橫向持股工作表。")
                holdings, details = (
                    investment_manager_core.load_xlsx_portfolio_horizontal_matrix(
                        import_snapshot,
                        sheet_configs=[
                            dict(item) for item in sheet_configs if isinstance(item, dict)
                        ],
                    )
                )
                sheet_name = "、".join(
                    str(item.get("sheet_name") or "")
                    for item in sheet_configs
                    if isinstance(item, dict) and item.get("enabled", True)
                )
                audit_event = "excel_horizontal_matrix_import"
                import_mode = "snapshot_horizontal_matrix"
            else:
                sheet_name = str(payload.get("sheet_name") or "").strip()
                if not sheet_name:
                    raise ValueError("請選擇 Excel 工作表。")
                header_row_number = payload.get("header_row_number")
                column_mapping = payload.get("column_mapping")
                if not isinstance(column_mapping, dict):
                    raise ValueError("請設定 Excel 欄位對應。")
                try:
                    parsed_header_row_number = int(header_row_number)
                except (TypeError, ValueError) as exc:
                    raise ValueError("標題列必須是正整數。") from exc
                raw_data_start_row_number = payload.get("data_start_row_number")
                try:
                    parsed_data_start_row_number = int(
                        parsed_header_row_number + 1
                        if raw_data_start_row_number is None
                        or raw_data_start_row_number == ""
                        else raw_data_start_row_number
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("資料起始列必須是正整數。") from exc
                holdings, details = investment_manager_core.load_xlsx_portfolio_with_mapping(
                    import_snapshot,
                    sheet_name=sheet_name,
                    header_row_number=parsed_header_row_number,
                    data_start_row_number=parsed_data_start_row_number,
                    column_mapping=column_mapping,
                )
                audit_event = "excel_column_mapping_import"
                import_mode = "snapshot_manual_mapping"
        finally:
            # Keep the verified source snapshot for audit/recovery.
            pass

        holding_dicts = [
            self._enrich_holding_principal_basis(self._holding_to_dict(holding))
            for holding in holdings
        ]
        self._assert_import_symbol_quality(holding_dicts)
        if (
            payload.get("_operation_id")
            and self._import_shutdown_event.is_set()
        ):
            raise _ImportResumePending("shutdown requested before import commit")
        profile = details.get("profile") if isinstance(details.get("profile"), dict) else {}
        workbook_scan = (
            details.get("workbook_scan")
            if isinstance(details.get("workbook_scan"), dict)
            else None
        )
        with self.repository.exclusive_data_access():
            previous_state = self.repository.load_state()
            previous_portfolio = (
                previous_state.get("portfolio")
                if isinstance(previous_state.get("portfolio"), dict)
                else {}
            )
            deduplicated = (
                previous_portfolio.get("import_fingerprint")
                == import_fingerprint
            )
            state = self.repository.save_portfolio(
                source,
                holding_dicts,
                workbook_scan=workbook_scan,
                excel_import_profile=profile,
                import_fingerprint=import_fingerprint,
            )
        if not deduplicated:
            self.analytics_store.audit(
                audit_event,
                {
                    "source_file": source.name,
                    "layout": layout,
                    "sheet_name": sheet_name,
                    "header_row_number": profile.get(
                        "header_row_number"
                    ),
                    "data_start_row_number": profile.get(
                        "data_start_row_number"
                    ),
                    "column_mapping": profile.get("column_mapping", {}),
                    "sheets": profile.get("sheets", []),
                    "imported_row_count": len(holding_dicts),
                    "skipped_row_count": details.get(
                        "skipped_row_count",
                        0,
                    ),
                    "source_file_modified": False,
                },
            )
        self._invalidate_v3_snapshot()
        local_risk_result: dict[str, Any] = {"ok": True, "queued": False}
        if payload.get("refresh_quotes", True) and not deduplicated:
            local_risk_result = self._schedule_local_risk_ai_background(
                state,
                {"trigger": audit_event, "live_quotes": True},
            )
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": (
                    f"已依 Excel 設定匯入 {len(holding_dicts)} 筆持股"
                    f"（略過 {details.get('skipped_row_count', 0)} 列）；原始 Excel 未修改。"
                ),
                "import_mode": import_mode,
                "deduplicated": deduplicated,
                "source_file_released": True,
                "source_file_modified": False,
                "excel_import_profile": profile,
                "imported_row_count": len(holding_dicts),
                "skipped_row_count": details.get("skipped_row_count", 0),
                "import_fingerprint": import_fingerprint,
                "local_risk_ai": local_risk_result,
            }
        )
        return response

    async def _read_portfolio_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_payload = dict(payload)
        worker_payload["_defer_local_risk"] = True
        response = await self._run_durable_worker(
            self._read_portfolio_file_sync,
            worker_payload,
        )
        state = response.get("state")
        if response.get("ok") is not False and isinstance(state, dict):
            local_risk_result = self._schedule_local_risk_ai_background(
                state,
                {
                    "trigger": "portfolio_import",
                    "live_quotes": True,
                },
            )
            if isinstance(local_risk_result.get("state"), dict):
                state = local_risk_result["state"]
                response["state"] = state
                response["diagnostics"] = await asyncio.to_thread(
                    self._diagnostics,
                    state,
                )
            response["product_status"] = local_risk_result.get("product_status")
            response["local_risk_ai"] = local_risk_result
        return response

    def _read_portfolio_file_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(
            payload.get("path")
            or payload.get("file_path")
            or payload.get("source_path")
            or ""
        ).strip()
        if not raw_path:
            return {"ok": False, "message": "請選擇要讀取的持股檔案。"}
        source = Path(raw_path).expanduser().resolve()
        if not source.exists() or not source.is_file():
            return {"ok": False, "message": f"找不到檔案：{source}"}
        if source.suffix.casefold() not in investment_manager_core.EXCEL_EXTENSIONS | investment_manager_core.CSV_EXTENSIONS | investment_manager_core.JSON_EXTENSIONS:
            return {
                "ok": False,
                "message": "只支援讀取 Excel、CSV 或 JSON 持股檔。",
            }
        import_snapshot = self._create_import_snapshot(source)
        try:
            source_digest = self._file_digest(import_snapshot)
            workbook_scan = None
            excel_import_profile = None
            import_mode = "snapshot"
            if source.suffix.casefold() in investment_manager_core.XLSX_EXTENSIONS:
                try:
                    holdings, details = (
                        investment_manager_core.load_xlsx_portfolio_consolidated_report(
                            import_snapshot
                        )
                    )
                    workbook_scan = details.get("workbook_scan")
                    excel_import_profile = details.get("profile")
                    import_mode = "snapshot_consolidated_report"
                except investment_manager_core.InvestmentManagerError as exc:
                    if "No consolidated return worksheet was detected" not in str(exc):
                        raise
                    workbook_scan = investment_manager_core.scan_xlsx_workbook(
                        import_snapshot
                    )
                    holdings = investment_manager_core.load_portfolio(import_snapshot)
            else:
                holdings = investment_manager_core.load_portfolio(import_snapshot)
        finally:
            # Keep the verified source snapshot for audit/recovery.
            pass
        holding_dicts = [self._holding_to_dict(holding) for holding in holdings]
        self._assert_import_symbol_quality(holding_dicts)
        import_fingerprint = self._portfolio_import_fingerprint(
            source_digest,
            {
                "source_path": str(source),
                "import_mode": import_mode,
                "excel_import_profile": excel_import_profile,
            },
        )
        with self.repository.exclusive_data_access():
            previous_state = self.repository.load_state()
            previous_portfolio = (
                previous_state.get("portfolio")
                if isinstance(previous_state.get("portfolio"), dict)
                else {}
            )
            deduplicated = (
                previous_portfolio.get("import_fingerprint")
                == import_fingerprint
            )
            state = self.repository.save_portfolio(
                source,
                holding_dicts,
                workbook_scan=workbook_scan,
                excel_import_profile=excel_import_profile,
                import_fingerprint=import_fingerprint,
            )
        local_risk_result: dict[str, Any] = {"ok": True, "queued": False}
        if not payload.get("_defer_local_risk") and not deduplicated:
            local_risk_result = self._schedule_local_risk_ai_background(
                state,
                {
                    "trigger": "portfolio_import",
                    "live_quotes": True,
                },
            )
        if isinstance(local_risk_result.get("state"), dict):
            state = local_risk_result["state"]
        selected_sheet = workbook_scan.get("selected_sheet") if workbook_scan else None
        sheet_note = ""
        if isinstance(selected_sheet, dict):
            sheet_note = (
                f"（{selected_sheet.get('sheet_name')}，"
                f"第 {selected_sheet.get('header_row_number')} 列欄位）"
            )
        return {
            "ok": True,
            "deduplicated": deduplicated,
            "message": (
                f"已讀取 {len(holding_dicts)} 筆持股{sheet_note}，"
                "原始檔已釋放，可繼續編輯；報價正在背景自動更新。"
            ),
            "import_mode": import_mode,
            "source_file_released": True,
            "state": state,
            "diagnostics": self._diagnostics(state),
            "mobile_sync": self._mobile_sync_status(),
            "workbook_scan": workbook_scan,
            "product_status": local_risk_result.get("product_status"),
            "local_risk_ai": local_risk_result,
        }

    async def _import_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._read_portfolio_file(payload)

    async def _clear_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        with self._snapshot_coordinator():
            safety_backup = self.analytics_store.clear_data()
            try:
                state = self.repository.clear_state()
            except Exception:
                self.analytics_store.restore_database(
                    Path(str(safety_backup["path"])).name
                )
                raise
        response = self._state_response(state)
        response["safety_backup"] = safety_backup
        response["message"] = "舊資料、交易帳本與分析歷史已刪除。"
        return response

    async def _export_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        diagnostics = self._diagnostics(state)
        analytics = self.analytics_store.analytics_snapshot(state)
        include_sensitive = bool(payload.get("include_sensitive"))
        if include_sensitive and not bool(payload.get("confirmed")):
            raise ValueError("匯出完整投資資料前必須明確確認，且檔案只會以帳號加密格式建立。")
        safe_diagnostics = {
            **diagnostics,
            "portfolio": {
                key: value
                for key, value in (diagnostics.get("portfolio") or {}).items()
                if key not in {"source_path", "file_name"}
            },
            "error_logging": {
                key: value
                for key, value in (diagnostics.get("error_logging") or {}).items()
                if key != "path"
            },
        }
        report = (
            {
                "tool": "AI投資管家",
                "version": self.VERSION,
                "local_only": True,
                "generated_at": diagnostics["generated_at"],
                "redaction_level": "encrypted_full_export",
                "diagnostics": diagnostics,
                "analytics": analytics,
                "state": {**state, "analytics": analytics},
            }
            if include_sensitive
            else {
                "tool": "AI投資管家",
                "version": self.VERSION,
                "local_only": True,
                "generated_at": diagnostics["generated_at"],
                "redaction_level": "support_bundle_default",
                "diagnostics": safe_diagnostics,
                "analytics": {
                    "version": analytics.get("version"),
                    "generated_at": analytics.get("generated_at"),
                    "data_health": analytics.get("data_health"),
                    "privacy": analytics.get("privacy"),
                    "risk_status": (analytics.get("risk") or {}).get("status"),
                    "calibration": analytics.get("calibration"),
                },
                "state": {
                    "portfolio": {
                        "holding_count": len(state.get("holdings") or []),
                        "imported_at": (state.get("portfolio") or {}).get("imported_at"),
                    },
                    "local_ai_product_status": state.get("local_ai_product_status"),
                    "local_ai_warning_count": len(state.get("local_ai_risk_warnings") or []),
                },
            }
        )
        export_root = self.repository.runtime_root / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        stamp = local_device_now().strftime("%Y%m%d_%H%M%S")
        report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        suffix = ".ivault" if include_sensitive else ".json"
        report_path = export_root / f"ai-investment-manager-report-{stamp}{suffix}"
        if include_sensitive:
            report_bytes = encode_binary_document(
                report_bytes,
                purpose="investment-support-bundle-full",
            )
        temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as output:
            output.write(report_bytes)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(report_path)
        return {
            "ok": True,
            "message": (
                f"完整投資資料已以目前 Windows 帳號加密匯出：{report_path}"
                if include_sensitive
                else f"已匯出預設去識別診斷報告：{report_path}"
            ),
            "report_path": str(report_path),
            "encrypted": include_sensitive,
            "redaction_level": report["redaction_level"],
            "diagnostics": diagnostics,
            "state": state,
            "mobile_sync": self._mobile_sync_status(),
        }

    async def _run_local_risk_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = await asyncio.to_thread(self.repository.load_state)
        return await asyncio.to_thread(
            self._run_local_risk_ai_for_state,
            state,
            dict(payload),
        )

    def _run_local_risk_ai_for_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.ai_connections is not None:
            return self._run_star_analysis_for_state(state, payload)
        if not state.get("holdings"):
            return {
                "ok": False,
                "message": "請先讀取 Excel 持股檔。",
                "state": state,
                "diagnostics": self._diagnostics(state),
                "mobile_sync": self._mobile_sync_status(),
            }
        return self._run_legacy_local_risk_ai_for_state(state, payload)

    def _run_star_analysis_for_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Production analysis boundary: Star searches and owns analysis.

        AI投資管家 supplies a minimal in-memory portfolio snapshot.  Neither
        Star nor external AI receives access to this tool's database.
        """

        holdings = [
            dict(item)
            for item in state.get("holdings", [])
            if isinstance(item, dict) and number(item.get("quantity"), 0) > 0
        ]
        if not holdings:
            return {
                "ok": False,
                "queued": False,
                "message": "請先讀取含有效數量的持股資料。",
                "state": state,
                "diagnostics": self._diagnostics(state),
                "mobile_sync": self._mobile_sync_status(),
            }
        run_id = str(payload.get("run_id") or "")
        instruction = str(payload.get("instruction") or payload.get("command") or "").strip()
        try:
            explicit_request = not bool(payload.get("trigger"))
            search = self.ai_connections.search_investments_sync(
                holdings,
                allow_external_fallback=(
                    explicit_request and payload.get("allow_external_research", True) is True
                ),
            )
            if not search.get("results"):
                raise RuntimeError(
                    str(search.get("message") or "星澄尚未連線；分析未送出且不排隊。")
                )
            quote_map = {
                (
                    str(item.get("market") or "").upper(),
                    str(item.get("requested_symbol") or "").upper(),
                ): item
                for item in search.get("results", [])
                if isinstance(item, dict) and item.get("trusted")
            }
            analysis_holdings: list[dict[str, Any]] = []
            for holding in holdings:
                compact = {
                    key: holding.get(key)
                    for key in (
                        "symbol",
                        "name",
                        "market",
                        "asset_type",
                        "currency",
                        "quantity",
                        "average_cost",
                        "principal_twd",
                        "current_value_twd",
                        "web_current_value_twd",
                    )
                }
                key = (
                    str(holding.get("market") or "").upper(),
                    str(holding.get("symbol") or "").upper(),
                )
                quote = quote_map.get(key)
                if isinstance(quote, dict):
                    source = next(
                        (entry for entry in quote.get("sources", []) if isinstance(entry, dict)),
                        {},
                    )
                    parameters = quote.get("parameters") if isinstance(quote.get("parameters"), dict) else {}
                    compact.update(
                        {
                            "web_current_price": parameters.get("price"),
                            "market_data_source": "星澄即時網路搜尋",
                            "market_data_source_url": source.get("url"),
                            "market_data_updated_at": quote.get("observed_at"),
                            "distribution": quote.get("distribution"),
                        }
                    )
                analysis_holdings.append(compact)
            policy = self._investment_policy()
            requested_parameters = payload.get("analysis_parameters")
            if not isinstance(requested_parameters, dict):
                requested_parameters = {}
            analysis_parameters = {
                "position_concentration_percent": requested_parameters.get(
                    "position_concentration_percent",
                    policy.get("max_single_position_percent", 20),
                ),
                "missing_data_warning_percent": requested_parameters.get(
                    "missing_data_warning_percent",
                    5,
                ),
                "instruction": instruction,
            }
            analysis = self.ai_connections.analyze_investments_sync(
                analysis_holdings,
                analysis_parameters,
            )
            if analysis.get("ok") is not True:
                raise RuntimeError(
                    str(analysis.get("message") or "星澄投資分析失敗。")
                )
            discuss_with_external_ai = (
                explicit_request
                and payload.get("discuss_with_external_ai", True) is True
            )
            discussion = (
                self.ai_connections.discuss_analysis_sync(analysis)
                if discuss_with_external_ai
                else {
                    "ok": False,
                    "queued": False,
                    "skipped": True,
                    "message": "背景分析不啟動外部 AI 瀏覽器。",
                    "uses_api": False,
                }
            )
            analysis["market_search"] = {
                "provider": search.get("provider"),
                "searched_at": search.get("searched_at"),
                "requested_count": search.get("requested_count"),
                "updated_count": search.get("updated_count"),
                "error_count": search.get("error_count"),
                "errors": search.get("errors", [])[:20],
            }
            analysis["external_ai_discussion"] = discussion
            warnings = analysis.get("risk_warnings") if isinstance(analysis.get("risk_warnings"), list) else []
            portfolio = analysis.get("portfolio") if isinstance(analysis.get("portfolio"), dict) else {}
            summary = {
                "warning_count": len(warnings),
                "active_holding_count": portfolio.get("active_holding_count", len(holdings)),
                "market_data_coverage_percent": portfolio.get("market_data_coverage_percent", 0),
                "analysis_owner": "星澄",
                "discussion_owner": "外部AI協作" if discussion.get("ok") else "未連線",
            }
            product_status = {
                "state": "ready" if not warnings else "warning",
                "state_label": "分析完成" if not warnings else "分析完成，有風險提醒",
                "analysis_owner": "星澄",
                "market_data_provider": "星澄即時網路搜尋",
                "external_discussion_connected": discussion.get("ok") is True,
                "queue_when_offline": False,
            }
            state_after_run = self.repository.save_local_ai_result(
                product_status,
                summary,
                warnings,
                None,
                full_analysis=analysis,
                explanation={
                    "mode": "star-analysis",
                    "mode_label": "星澄分析",
                    "text": "市場搜尋與投資分析由星澄執行；外部 AI 僅討論分析結果。",
                },
            )
            prompt = "星澄：搜尋可驗證市場資料並執行投資分析。"
            if instruction:
                prompt += f" 使用者指令：{instruction}"
            if run_id:
                run = self.repository.update_ai_run(
                    run_id,
                    status="completed",
                    content=json.dumps(summary, ensure_ascii=False),
                    error="",
                )
            else:
                run = self.repository.add_ai_run(
                    role="investment_analysis",
                    provider="星澄",
                    prompt=prompt,
                    status="completed",
                    content=json.dumps(summary, ensure_ascii=False),
                    error="",
                )
            return {
                "ok": True,
                "queued": False,
                "message": f"星澄分析完成：{len(warnings)} 項風險提醒。",
                "run": run,
                "summary": summary,
                "product_status": product_status,
                "risk_warnings": warnings,
                "local_risk_ai": analysis,
                "external_ai_discussion": discussion,
                "state": self._state_with_analytics(state_after_run),
                "diagnostics": self._diagnostics(state_after_run),
                "mobile_sync": self._mobile_sync_status(),
            }
        except Exception as exc:
            if run_id:
                run = self.repository.update_ai_run(
                    run_id,
                    status="failed",
                    content="",
                    error=str(exc),
                )
            else:
                run = self.repository.add_ai_run(
                    role="investment_analysis",
                    provider="星澄",
                    prompt="星澄投資分析",
                    status="failed",
                    content="",
                    error=str(exc),
                )
            latest = self.repository.load_state()
            return {
                "ok": False,
                "queued": False,
                "message": str(exc),
                "run": run,
                "state": latest,
                "diagnostics": self._diagnostics(latest),
                "mobile_sync": self._mobile_sync_status(),
            }
    def _run_legacy_local_risk_ai_for_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Compatibility path used by isolated unit tests and legacy callers."""

        if self.model_governance.is_read_only("local-risk-ai"):
            run_id = str(payload.get("run_id") or "")
            if run_id:
                self.repository.update_ai_run(
                    run_id,
                    status="failed",
                    error="模型校準已觸發唯讀護欄。",
                )
            return {
                "ok": False,
                "message": "模型校準已觸發唯讀護欄；請完成模型覆核或新版本驗證後再產生建議。",
                "state": self._state_with_analytics(state),
                "diagnostics": self._diagnostics(state),
                "mobile_sync": self._mobile_sync_status(),
                "model_guardrail": "read_only",
            }
        instruction = str(payload.get("instruction") or payload.get("command") or "").strip()
        live_quotes = bool(payload.get("live_quotes", True))
        prompt = (
            "本地輔助AI：自動連網抓取公開報價，監測股價、持倉成本與集中度風險"
            "（本地推理，非投資建議）"
        )
        if instruction:
            prompt = f"{prompt}\n使用者命令：{instruction}"
        try:
            analysis_state = dict(state)
            analysis_state["investment_policy"] = self._investment_policy()
            performance_context = self.analytics_store.performance(state)
            analysis_state["analytics_context"] = {
                "generated_at": local_device_now().isoformat(),
                "risk": self.analytics_store.risk(state),
                "ledger": performance_context.get("ledger", {}),
                "recent_events": self.analytics_store.list_events(30),
                "decision_journal": self.analytics_store.decisions(20),
                "provenance": {
                    "portfolio": "local encrypted state",
                    "ledger": "local encrypted analytics database",
                    "events": "local event store with per-record source",
                    "risk": "deterministic local analytics",
                },
            }
            quote_function = (
                self._quote_function_with_cached_market_data(state)
                if live_quotes
                else None
            )
            result = local_risk_ai.analyze_state(
                analysis_state,
                live_quotes=live_quotes,
                instruction=instruction,
                quote_function=quote_function,
                previous_analysis=(
                    state.get("local_ai_analysis_cache")
                    if isinstance(state.get("local_ai_analysis_cache"), dict)
                    else None
                ),
            )
            calibration = self.analytics_store.calibration()
            self._apply_decision_calibration(result, calibration)
            explanation = self.local_explanation_engine.explain(result, calibration)
            result["explanation"] = explanation
            summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
            network_context = (
                result.get("network_context")
                if isinstance(result.get("network_context"), dict)
                else {}
            )
            product_status = (
                result.get("product_status")
                if isinstance(result.get("product_status"), dict)
                else None
            )
            if isinstance(product_status, dict):
                product_status["explanation_mode"] = explanation.get("mode")
                product_status["explanation_mode_label"] = explanation.get("mode_label")
                product_status["explanation_text"] = explanation.get("text")
                product_status["calibration"] = calibration
            if not self._portfolio_signature_matches(payload):
                run_id = str(payload.get("run_id") or "")
                if run_id:
                    self.repository.update_ai_run(
                        run_id,
                        status="failed",
                        content="",
                        error="持股已更新，本次背景報價結果已略過。",
                    )
                current_state = self.repository.load_state()
                return {
                    "ok": False,
                    "message": "持股已更新，本次背景報價結果已略過。",
                    "state": current_state,
                    "diagnostics": self._diagnostics(current_state),
                    "mobile_sync": self._mobile_sync_status(),
                }
            state_after_run = self.repository.save_local_ai_result(
                product_status,
                summary,
                result.get("risk_warnings") if isinstance(result.get("risk_warnings"), list) else [],
                result.get("command_result")
                if isinstance(result.get("command_result"), dict)
                else None,
                full_analysis=result,
                explanation=explanation,
            )
            self.analytics_store.record_analysis_snapshot(result, state_after_run)
            self.analytics_store.record_decisions(result)
            self.model_governance.record_run(
                model_name="local-risk-ai",
                version=str(result.get("model_version") or self.VERSION),
                inputs={"portfolio_signature": self._portfolio_signature(state), "instruction": instruction},
                outputs={"summary": summary, "warnings": result.get("risk_warnings", [])},
                data_sources=["local portfolio", "public market quotes"] if live_quotes else ["local portfolio"],
                prompt=prompt,
                analysis_run_id=str(payload.get("run_id") or ""),
                decision_count=int(summary.get("warning_count") or 0),
            )
            self._invalidate_v3_snapshot()
            run_id = str(payload.get("run_id") or "")
            if run_id:
                run = self.repository.update_ai_run(
                    run_id,
                    status="completed",
                    content=str(result.get("content") or ""),
                    error="",
                )
            else:
                run = self.repository.add_ai_run(
                    role="local_risk_monitor",
                    provider="local-risk-ai",
                    prompt=prompt,
                    status="completed",
                    content=str(result.get("content") or ""),
                    error="",
                )
            status_label = ""
            if isinstance(product_status, dict):
                status_label = str(product_status.get("state_label") or "").strip()
            message = f"本地輔助AI完成：{summary.get('warning_count', 0)} 項風險預告。"
            if status_label:
                message = f"{message} 狀態：{status_label}。"
            coverage_label = str(network_context.get("coverage_label") or "").strip()
            if coverage_label:
                health_label = str(network_context.get("health_label") or "").strip()
                health_suffix = f"，{health_label}" if health_label else ""
                message = f"{message} 報價：{coverage_label}{health_suffix}。"
            return {
                "ok": True,
                "message": message,
                "run": run,
                "summary": summary,
                "product_status": product_status,
                "risk_warnings": result.get("risk_warnings", []),
                "local_risk_ai": result,
                "state": self._state_with_analytics(state_after_run),
                "diagnostics": self._diagnostics(state_after_run),
                "mobile_sync": self._mobile_sync_status(),
            }
        except Exception as exc:
            run_id = str(payload.get("run_id") or "")
            if run_id:
                run = self.repository.update_ai_run(
                    run_id,
                    status="failed",
                    content="",
                    error=str(exc),
                )
            else:
                run = self.repository.add_ai_run(
                    role="local_risk_monitor",
                    provider="local-risk-ai",
                    prompt=prompt,
                    status="failed",
                    content="",
                    error=str(exc),
                )
            state_after_error = self.repository.load_state()
            return {
                "ok": False,
                "message": str(exc),
                "run": run,
                "state": state_after_error,
                "diagnostics": self._diagnostics(state_after_error),
                "mobile_sync": self._mobile_sync_status(),
            }

    @staticmethod
    def _apply_decision_calibration(
        result: dict[str, Any],
        calibration: dict[str, Any],
    ) -> None:
        multiplier = number(calibration.get("confidence_multiplier"), 1.0)
        if multiplier <= 0:
            multiplier = 1.0
        command = result.get("command_result") if isinstance(result.get("command_result"), dict) else {}
        assessment = result.get("local_ai_assessment") if isinstance(result.get("local_ai_assessment"), dict) else {}
        summaries = []
        for parent in (command, assessment):
            summary = parent.get("confidence_summary") if isinstance(parent.get("confidence_summary"), dict) else None
            if isinstance(summary, dict) and summary not in summaries:
                summaries.append(summary)
        for summary in summaries:
            raw_score = number(summary.get("score"), 0.0)
            calibrated = max(0.0, min(1.0, raw_score * multiplier))
            summary["raw_score"] = round(raw_score, 4)
            summary["score"] = round(calibrated, 4)
            summary["calibration_multiplier"] = round(multiplier, 4)
            summary["calibration_sample_count"] = int(calibration.get("evaluated_count") or 0)
            summary["label"] = "高" if calibrated >= 0.8 else "中" if calibrated >= 0.55 else "低"
        product = result.get("product_status") if isinstance(result.get("product_status"), dict) else {}
        if summaries and product:
            product["confidence_score"] = summaries[0].get("score")
            product["confidence_label"] = summaries[0].get("label")
            product["calibration_applied"] = bool(calibration.get("evaluated_count"))

    @staticmethod
    def _quote_function_with_cached_market_data(
        state: dict[str, Any],
    ) -> Any:
        cached_by_key: dict[tuple[str, str, int | None], dict[str, Any]] = {}
        cached_by_symbol: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in state.get("holdings", []):
            if not isinstance(raw, dict):
                continue
            market = str(raw.get("market") or "").strip().upper()
            symbol = str(raw.get("symbol") or "").strip().upper()
            source_row = raw.get("source_row")
            row_number = int(source_row) if isinstance(source_row, int) else None
            cached_by_key[(market, symbol, row_number)] = raw
            cached_by_symbol[(market, symbol)] = raw

        def quote_with_cache(
            holding: investment_manager_core.Holding,
            providers: dict[str, Any],
            provider_order: list[str],
            now: datetime,
        ) -> tuple[
            investment_manager_core.Quote | None,
            list[investment_manager_core.QuoteAttempt],
            list[investment_manager_core.Quote],
        ]:
            key = (holding.market.upper(), holding.symbol.upper(), holding.source_row)
            raw = cached_by_key.get(key) or cached_by_symbol.get(key[:2]) or {}
            price = number(raw.get("web_current_price"), 0)
            observed_at = str(raw.get("market_data_updated_at") or "").strip()
            provider = str(raw.get("market_data_source") or "公開市場報價")
            market_state = "CACHED"
            if price <= 0 and holding.market.upper() == "FUND" and holding.quantity > 0:
                current_value_twd = number(raw.get("current_value_twd"), 0)
                fx_rate = (
                    1.0
                    if holding.currency.upper() in {"", "TWD"}
                    else number(raw.get("average_cost_fx_rate"), 0)
                )
                if current_value_twd > 0 and fx_rate > 0:
                    price = current_value_twd / holding.quantity / fx_rate
                    observed_at = str(
                        (state.get("portfolio") or {}).get("imported_at")
                        or state.get("updated_at")
                        or now.isoformat()
                    )
                    provider = "Excel 報酬工作表快照"
                    market_state = "SNAPSHOT_ONLY"
            if price > 0 and observed_at:
                quote = investment_manager_core.Quote(
                    symbol=holding.symbol,
                    requested_symbol=holding.symbol,
                    provider=provider,
                    price=price,
                    currency=str(
                        raw.get("web_current_price_currency")
                        or holding.currency
                        or ""
                    ).upper(),
                    as_of=observed_at,
                    market_state=market_state,
                    exchange=provider,
                    raw_market=holding.market,
                )
                return (
                    quote,
                    [
                        investment_manager_core.QuoteAttempt(
                            provider,
                            True,
                            "使用最近一次已同步報價。",
                        )
                    ],
                    [quote],
                )
            provider_holding = holding
            fund_quote_symbol = str(raw.get("fund_quote_symbol") or "").strip().upper()
            if (
                holding.market.upper() == "FUND"
                and fund_quote_symbol
                and str(raw.get("fund_identity_status") or "") == "confirmed"
            ):
                provider_holding = investment_manager_core.Holding(
                    symbol=fund_quote_symbol,
                    name=holding.name,
                    market="US",
                    asset_type="FUND",
                    quantity=holding.quantity,
                    average_cost=holding.average_cost,
                    currency=holding.currency,
                    source_row=holding.source_row,
                )
            quote, attempts, candidates = investment_manager_core.quote_holding_candidates(
                provider_holding,
                providers,
                provider_order,
                now,
                max_successes=2,
            )
            if provider_holding is not holding and quote is not None:
                def relabel(source_quote: investment_manager_core.Quote) -> investment_manager_core.Quote:
                    return investment_manager_core.Quote(
                        symbol=holding.symbol,
                        requested_symbol=holding.symbol,
                        provider=f"{source_quote.provider} · {fund_quote_symbol}",
                        price=source_quote.price,
                        currency=source_quote.currency,
                        previous_close=source_quote.previous_close,
                        change=source_quote.change,
                        change_percent=source_quote.change_percent,
                        as_of=source_quote.as_of,
                        market_state=source_quote.market_state,
                        exchange=source_quote.exchange,
                        raw_market=holding.market,
                    )
                return relabel(quote), attempts, [relabel(item) for item in candidates]
            return quote, attempts, candidates

        return quote_with_cache

    def _schedule_local_risk_ai_background(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "本地輔助AI：持股更新後背景自動連網報價與風險監測（本地推理）"
            if payload.get("trigger") == "manual_holding_change"
            else "本地輔助AI：Excel 匯入後背景自動連網報價與風險監測（本地推理）"
        )
        run = self.repository.add_ai_run(
            role="local_risk_monitor",
            provider="local-risk-ai",
            prompt=prompt,
            status="running",
            content="",
            error="",
        )
        state_with_run = self.repository.load_state()
        background_payload = {
            **payload,
            "run_id": run["run_id"],
            "portfolio_signature": self._portfolio_signature(state_with_run),
            "allow_external_research": False,
            "discuss_with_external_ai": False,
        }
        task = asyncio.create_task(
            asyncio.to_thread(
                self._run_local_risk_ai_for_state,
                state_with_run,
                background_payload,
            )
        )
        self._background_jobs.add(task)
        task.add_done_callback(self._background_job_finished)
        return {
            "ok": True,
            "queued": True,
            "message": "本地輔助AI已排入背景，自動連網抓取報價。",
            "run": run,
            "state": state_with_run,
            "product_status": state_with_run.get("local_ai_product_status"),
        }

    def _portfolio_signature_matches(self, payload: dict[str, Any]) -> bool:
        expected = str(payload.get("portfolio_signature") or "")
        if not expected:
            return True
        return expected == self._portfolio_signature(self.repository.load_state())

    @staticmethod
    def _portfolio_signature(state: dict[str, Any]) -> str:
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        return "|".join(
            [
                str(portfolio.get("source_path") or ""),
                str(portfolio.get("imported_at") or ""),
                str(portfolio.get("holding_count") or ""),
                str(portfolio.get("manual_revision") or ""),
                str(portfolio.get("manually_modified_at") or ""),
            ]
        )

    def _mobile_sync_remote_url(self) -> str:
        env_url = str(os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_SYNC_URL") or "").strip()
        return env_url or self.repository.mobile_sync_remote_url()

    def _mobile_sync_status(self, *, expose_pairing_code: bool = True) -> dict[str, Any]:
        status = self.mobile_sync_gateway.status(
            expose_pairing_code=expose_pairing_code
        )
        status["start_error"] = self._mobile_sync_start_error
        return status

    def _mobile_sync_snapshot(self) -> dict[str, Any]:
        state = self.repository.load_state()
        analytics = self.analytics_store.analytics_snapshot(state)
        diagnostics = self._diagnostics(state)
        return {
            "ok": True,
            "tool": "AI投資管家",
            "version": self.VERSION,
            "generated_at": local_device_now().isoformat(),
            "platform": mobile_platform_contract(),
            "sync": self._mobile_sync_status(expose_pairing_code=False),
            "state": self._compact_mobile_state(state),
            "analytics": {
                "performance": analytics.get("performance"),
                "risk": analytics.get("risk"),
                "stress": analytics.get("stress"),
                "alerts": analytics.get("alerts"),
                "calibration": analytics.get("calibration"),
            },
            "diagnostics": diagnostics,
            "local_only": True,
        }

    def _schedule_mobile_local_ai_command(self, instruction: str) -> dict[str, Any]:
        if not instruction.strip():
            return {"ok": False, "message": "請輸入本地 AI 命令"}
        if self._event_loop is None or self._event_loop.is_closed():
            return {"ok": False, "message": "手機同步服務尚未連到本地 AI 執行迴圈"}
        future = asyncio.run_coroutine_threadsafe(
            self._queue_mobile_local_ai_command(instruction),
            self._event_loop,
        )
        return future.result(timeout=10)

    async def _queue_mobile_local_ai_command(self, instruction: str) -> dict[str, Any]:
        state = self.repository.load_state()
        if not state.get("holdings"):
            return {
                "ok": False,
                "message": "請先在桌面端讀取持股檔案",
                "sync": self._mobile_sync_status(expose_pairing_code=False),
            }
        result = self._schedule_local_risk_ai_background(
            state,
            {
                "trigger": "mobile_remote_command",
                "instruction": instruction,
                "live_quotes": True,
            },
        )
        return {
            "ok": bool(result.get("ok")),
            "queued": bool(result.get("queued")),
            "message": result.get("message") or "本地 AI 命令已排入背景執行",
            "run": result.get("run"),
            "sync": self._mobile_sync_status(expose_pairing_code=False),
        }

    @staticmethod
    def _compact_mobile_state(state: dict[str, Any]) -> dict[str, Any]:
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else None
        if isinstance(portfolio, dict):
            portfolio = {
                "file_name": portfolio.get("file_name") or "",
                "holding_count": portfolio.get("holding_count") or 0,
                "imported_at": portfolio.get("imported_at") or "",
            }

        holdings: list[dict[str, Any]] = []
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            holdings.append(
                {
                    "symbol": holding.get("symbol") or "",
                    "name": holding.get("name") or "",
                    "market": holding.get("market") or "",
                    "asset_type": holding.get("asset_type") or "",
                    "quantity": holding.get("quantity") or 0,
                    "average_cost": holding.get("average_cost"),
                    "currency": holding.get("currency") or "",
                }
            )

        runs: list[dict[str, Any]] = []
        for run in state.get("ai_runs", []):
            if not isinstance(run, dict):
                continue
            runs.append(
                {
                    "run_id": run.get("run_id") or "",
                    "role": run.get("role") or "",
                    "provider": run.get("provider") or "",
                    "status": run.get("status") or "",
                    "created_at": run.get("created_at") or "",
                    "finished_at": run.get("finished_at") or "",
                    "content": InvestmentWatchRepository._shorten(
                        str(run.get("content") or ""),
                        1200,
                    ),
                    "error": InvestmentWatchRepository._shorten(
                        str(run.get("error") or ""),
                        600,
                    ),
                }
            )
            if len(runs) >= 12:
                break

        command_result = (
            state.get("local_ai_command_result")
            if isinstance(state.get("local_ai_command_result"), dict)
            else None
        )
        return {
            "portfolio": portfolio,
            "holdings": holdings,
            "workbook_scan_quality": state.get("workbook_scan_quality"),
            "local_ai_product_status": state.get("local_ai_product_status"),
            "local_ai_summary": state.get("local_ai_summary"),
            "local_ai_risk_warnings": list(state.get("local_ai_risk_warnings", []))[:20],
            "local_ai_command_result": command_result,
            "local_ai_action_plan": list(state.get("local_ai_action_plan", []))[:8],
            "local_ai_watch_triggers": list(state.get("local_ai_watch_triggers", []))[:12],
            "local_ai_confidence": state.get("local_ai_confidence"),
            "local_ai_decision_brief": state.get("local_ai_decision_brief") or "",
            "local_ai_network_context": state.get("local_ai_network_context"),
            "shared_memory": InvestmentWatchRepository._shorten(
                str(state.get("shared_memory") or ""),
                8000,
            ),
            "ai_runs": runs,
            "updated_at": state.get("updated_at") or "",
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
            state.get("local_ai_product_status")
            if isinstance(state.get("local_ai_product_status"), dict)
            else {}
        )
        summary = (
            state.get("local_ai_summary")
            if isinstance(state.get("local_ai_summary"), dict)
            else {}
        )
        risk_warnings = [
            item
            for item in state.get("local_ai_risk_warnings", [])
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
        local_ai_state = str(product_status.get("state") or "")
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
            message = "請讀取 Excel 持股檔，本地輔助 AI 會自動建立風險監測。"
        elif mapping_error:
            state_key = "critical"
            state_label = "Excel 欄位需修正"
            message = (
                f"Excel 欄位映射錯誤：{len(holdings)} 筆中有 {invalid_symbol_count} 筆代號"
                "看起來像價格或無效值；投資風險判讀已暫停，請用「Excel 欄位」重新匯入。"
            )
        elif critical_count > 0 or local_ai_state == "critical" or workbook_state == "critical":
            state_key = "critical"
            state_label = "需要立即檢查"
            message = "已偵測重大風險或 Excel 掃描品質不足，請先確認資料與部位上限。"
        elif stale or warning_count > 0 or local_ai_state == "attention" or workbook_state == "attention":
            state_key = "attention"
            state_label = "需要關注"
            message = "持股資料、本地 AI 或掃描品質有待確認項目，建議重新評估。"
        else:
            state_key = "ready"
            state_label = "監測正常"
            message = "本地輔助 AI 已完成持股監測，資料狀態正常。"

        selected_sheet = (
            workbook_scan.get("selected_sheet")
            if isinstance(workbook_scan.get("selected_sheet"), dict)
            else {}
        )
        latest_run = runs[0] if runs else {}
        network_context = (
            state.get("local_ai_network_context")
            if isinstance(state.get("local_ai_network_context"), dict)
            else product_status.get("network_context")
            if isinstance(product_status.get("network_context"), dict)
            else {}
        )
        error_log_path = self.repository.runtime_root / "local-ai-errors.jsonl"
        error_archive_root = (
            self.repository.runtime_root / "local-ai-error-archives"
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
            "local_ai": {
                "state": local_ai_state or ("empty" if not holdings else "attention"),
                "state_label": product_status.get("state_label")
                or ("等待持股資料" if not holdings else "等待本地 AI 分析"),
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
