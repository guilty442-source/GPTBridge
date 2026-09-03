from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import portfolio_file
from .mapping_repair import build_smart_mapping_repair
from .privacy import encode_binary_document

investment_manager_core = portfolio_file


class _ImportResumePending(RuntimeError):
    """Internal signal: parsing may be retried, but no state commit was made."""


class InvestmentImportServiceMixin:
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
        if payload.get("confirmed") is not True:
            raise ValueError("刪除投資資料前必須明確確認")
        permanent = payload.get("permanent") is True
        with self._snapshot_coordinator():
            safety_backup = self.analytics_store.clear_data(permanent=permanent)
            try:
                state = self.repository.clear_state(permanent=permanent)
            except Exception:
                if not permanent:
                    self.analytics_store.restore_database(
                        Path(str(safety_backup["path"])).name
                    )
                raise
        response = self._state_response(state)
        response["safety_backup"] = safety_backup
        response["permanent"] = permanent
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
                    "xingcheng_product_status": state.get("xingcheng_product_status"),
                    "xingcheng_warning_count": len(state.get("xingcheng_risk_warnings") or []),
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
