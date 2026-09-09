from __future__ import annotations

from typing import Any

from .watch_repo_helpers import utc_now
from .watch_repo_utils import WatchRepoUtilsMixin


class WatchRepoSharedMixin:
    """Shared memory generation and workbook scan quality assessment."""

    def _shared_memory(self, state: dict[str, Any]) -> str:
        lines = [
            "投資看盤共用記憶",
            f"updated_at: {state.get('updated_at') or utc_now()}",
            "",
            "Portfolio memory:",
            str(state.get("portfolio_memory") or "尚未匯入持股。"),
        ]
        product_status = state.get("xingcheng_product_status")
        if isinstance(product_status, dict):
            network_context = (
                product_status.get("network_context")
                if isinstance(product_status.get("network_context"), dict)
                else {}
            )
            lines.extend(
                [
                    "",
                    "Local AI product status:",
                    (
                        f"{product_status.get('state_label', '-')} "
                        f"score={product_status.get('score', '-')} "
                        f"mode={product_status.get('watch_status', '-')} "
                        f"network={product_status.get('network_mode_label', '-')} "
                        f"coverage={product_status.get('coverage_label', '-')} "
                        f"providers={network_context.get('quote_provider_count', 0)}"
                    ),
                    str(product_status.get("recommendation") or ""),
                ]
            )
        network_context = state.get("xingcheng_network_context")
        if isinstance(network_context, dict):
            lines.extend(
                [
                    "",
                    "Local AI network context:",
                    (
                        f"{network_context.get('mode_label', '-')} "
                        f"enabled={network_context.get('enabled', False)} "
                        f"coverage={network_context.get('coverage_label', '-')} "
                        f"providers={network_context.get('quote_providers', [])}"
                    ),
                ]
            )
        decision_brief = str(state.get("xingcheng_decision_brief") or "").strip()
        if decision_brief:
            lines.extend(["", "Local AI decision brief:", decision_brief])
        action_plan = state.get("xingcheng_action_plan")
        if isinstance(action_plan, list) and action_plan:
            lines.extend(["", "Local AI action plan:"])
            for item in action_plan[:6]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    (
                        f"{item.get('priority', '-')} {item.get('symbol', '-')} "
                        f"{item.get('title', '-')} -> {item.get('action', '-')}"
                    )
                )
        confidence = state.get("xingcheng_confidence")
        if isinstance(confidence, dict):
            lines.extend(
                [
                    "",
                    "Local AI confidence:",
                    (
                        f"{confidence.get('label', '-')} "
                        f"score={confidence.get('score', '-')} "
                        f"low={confidence.get('low_confidence_symbols', [])}"
                    ),
                ]
            )
        workbook_quality = state.get("workbook_scan_quality")
        if isinstance(workbook_quality, dict):
            lines.extend(
                [
                    "",
                    "Workbook scan quality:",
                    (
                        f"{workbook_quality.get('state_label', '-')} "
                        f"score={workbook_quality.get('score', '-')} "
                        f"sheet={workbook_quality.get('selected_sheet_name', '-')}"
                    ),
                    str(workbook_quality.get("recommendation") or ""),
                ]
            )
        runs = state.get("ai_runs", [])
        if isinstance(runs, list) and runs:
            lines.extend(["", "Primary AI results, latest first:"])
            for run in runs[:12]:
                if not isinstance(run, dict):
                    continue
                provider = str(run.get("provider") or "unknown")
                role = str(run.get("role") or "unknown")
                status = str(run.get("status") or "unknown")
                created_at = str(run.get("created_at") or "")
                body = str(run.get("content") or run.get("error") or "").strip()
                lines.extend(
                    [
                        "",
                        f"[{provider}] role={role} status={status} created_at={created_at}",
                        self._shorten(body, 2200),
                    ]
                )
        return self._shorten("\n".join(lines), 40000)

    @staticmethod
    def _compact_workbook_scan(workbook_scan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(workbook_scan, dict):
            return None
        selected_sheet = workbook_scan.get("selected_sheet")
        sheets = workbook_scan.get("sheets")
        return {
            "sheet_count": workbook_scan.get("sheet_count", 0),
            "selected_sheet": selected_sheet if isinstance(selected_sheet, dict) else None,
            "sheets": list(sheets)[:20] if isinstance(sheets, list) else [],
        }

    @staticmethod
    def _workbook_scan_quality(workbook_scan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(workbook_scan, dict):
            return None
        selected_sheet = workbook_scan.get("selected_sheet")
        if not isinstance(selected_sheet, dict):
            return {
                "state": "critical",
                "state_label": "未辨識",
                "score": 0,
                "selected_sheet_name": "",
                "recommendation": "活頁簿未找到可辨識的持股表，請確認有代號欄位或可推斷的持股資料列。",
            }

        score = WatchRepoUtilsMixin._int_value(selected_sheet.get("score"))
        valid_rows = WatchRepoUtilsMixin._int_value(
            selected_sheet.get("valid_data_row_count")
        )
        header_mode = str(selected_sheet.get("header_mode") or "")
        header_depth = WatchRepoUtilsMixin._int_value(
            selected_sheet.get("header_depth"),
            default=1,
        )
        if score >= 180 and valid_rows > 0:
            state = "ready"
            state_label = "辨識穩定"
        elif score >= 120 and valid_rows > 0:
            state = "attention"
            state_label = "需確認"
        else:
            state = "critical"
            state_label = "低信心"

        if header_mode == "horizontal_matrix":
            recommendation = "已合併橫向持股工作表；ETF、台股、美股與共同基金欄位可個別調整。"
        elif header_mode == "headerless_inferred":
            recommendation = "偵測為無表頭自製表格，已從資料列推斷代號、數量、平均成本等欄位。"
        elif header_mode == "inferred":
            recommendation = "已依資料形態推斷自製表格欄位，建議確認代號、數量、平均成本是否對齊。"
        elif header_depth > 1:
            recommendation = "已合併多列自製表頭，建議確認欄位合併後名稱是否符合原表。"
        else:
            recommendation = "已辨識持股欄位，匯入後會自動交給本地AI建立監測基準。"

        return {
            "state": state,
            "state_label": state_label,
            "score": score,
            "selected_sheet_name": str(selected_sheet.get("sheet_name") or ""),
            "header_row_number": selected_sheet.get("header_row_number"),
            "header_mode": header_mode,
            "header_depth": header_depth,
            "valid_data_row_count": valid_rows,
            "recommendation": recommendation,
        }
