from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .portfolio_file import local_device_now
from .privacy import encode_binary_document


class ImportReportsMixin:
    """State clearing and support-bundle report exports."""

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
        report = self._support_report(
            state,
            diagnostics,
            analytics,
            include_sensitive,
        )
        report_path = self._write_export_report(report, include_sensitive)
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

    @staticmethod
    def _redacted_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
        return {
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

    def _support_report(
        self,
        state: dict[str, Any],
        diagnostics: dict[str, Any],
        analytics: dict[str, Any],
        include_sensitive: bool,
    ) -> dict[str, Any]:
        if include_sensitive:
            return {
                "tool": "投資管家",
                "version": self.VERSION,
                "local_only": True,
                "generated_at": diagnostics["generated_at"],
                "redaction_level": "encrypted_full_export",
                "diagnostics": diagnostics,
                "analytics": analytics,
                "state": {**state, "analytics": analytics},
            }
        return {
            "tool": "投資管家",
            "version": self.VERSION,
            "local_only": True,
            "generated_at": diagnostics["generated_at"],
            "redaction_level": "support_bundle_default",
            "diagnostics": self._redacted_diagnostics(diagnostics),
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
                "ollama_product_status": state.get("ollama_product_status"),
                "ollama_warning_count": len(state.get("ollama_risk_warnings") or []),
            },
        }

    def _write_export_report(
        self,
        report: dict[str, Any],
        include_sensitive: bool,
    ) -> Path:
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
        return report_path
