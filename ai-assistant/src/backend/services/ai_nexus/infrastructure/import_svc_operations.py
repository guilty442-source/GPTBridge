from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from .import_svc_excel import _ImportResumePending
from ..application.watch_repository import InvestmentWatchRepository


class ImportSvcOperationsMixin:
    """Import operation lifecycle management for InvestmentImportServiceMixin."""

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
