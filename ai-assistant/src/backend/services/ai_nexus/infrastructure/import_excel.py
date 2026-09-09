from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from . import portfolio_file as investment_manager_core
from .mapping_repair import build_smart_mapping_repair


class _ImportResumePending(RuntimeError):
    """Internal signal: parsing may be retried, but no state commit was made."""


class ImportExcelMixin:
    """Excel mapping source resolution, preview, and import execution."""

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
