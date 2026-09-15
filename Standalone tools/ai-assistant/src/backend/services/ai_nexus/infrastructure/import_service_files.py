from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from . import portfolio_file as investment_manager_core


class ImportFilesMixin:
    """Portfolio file reading, durable workers, and import commits."""

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

    @staticmethod
    def _portfolio_source(
        payload: dict[str, Any],
    ) -> tuple[Path | None, dict[str, Any] | None]:
        raw_path = str(
            payload.get("path")
            or payload.get("file_path")
            or payload.get("source_path")
            or ""
        ).strip()
        if not raw_path:
            return None, {"ok": False, "message": "請選擇要讀取的持股檔案。"}
        source = Path(raw_path).expanduser().resolve()
        if not source.exists() or not source.is_file():
            return None, {"ok": False, "message": f"找不到檔案：{source}"}
        if source.suffix.casefold() not in investment_manager_core.EXCEL_EXTENSIONS | investment_manager_core.CSV_EXTENSIONS | investment_manager_core.JSON_EXTENSIONS:
            return None, {
                "ok": False,
                "message": "只支援讀取 Excel、CSV 或 JSON 持股檔。",
            }
        return source, None

    def _read_portfolio_file_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        source, error = self._portfolio_source(payload)
        if error is not None:
            return error
        import_snapshot = self._create_import_snapshot(source)
        source_digest = self._file_digest(import_snapshot)
        holdings, workbook_scan, excel_import_profile, import_mode = (
            self._load_snapshot_holdings(source, import_snapshot)
        )
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
        state, deduplicated = self._commit_portfolio_import(
            source,
            holding_dicts,
            workbook_scan,
            excel_import_profile,
            import_fingerprint,
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
        return self._import_read_result(
            holding_dicts,
            deduplicated,
            import_mode,
            state,
            workbook_scan,
            local_risk_result,
        )

    @staticmethod
    def _load_snapshot_holdings(
        source: Path,
        import_snapshot: Path,
    ) -> tuple[Any, Any, Any, str]:
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
        return holdings, workbook_scan, excel_import_profile, import_mode

    def _commit_portfolio_import(
        self,
        source: Path,
        holding_dicts: list[dict[str, Any]],
        workbook_scan: Any,
        excel_import_profile: Any,
        import_fingerprint: str,
    ) -> tuple[dict[str, Any], bool]:
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
        return state, deduplicated

    def _import_read_result(
        self,
        holding_dicts: list[dict[str, Any]],
        deduplicated: bool,
        import_mode: str,
        state: dict[str, Any],
        workbook_scan: Any,
        local_risk_result: dict[str, Any],
    ) -> dict[str, Any]:
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
