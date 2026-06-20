from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvestmentWatchRepository:
    def __init__(self, tool_root: Path) -> None:
        self.tool_root = tool_root.resolve()
        self.runtime_root = self.tool_root / "runtime"
        self.state_root = self.runtime_root / "state"
        self.state_path = self.state_root / "investment_watch_state.json"
        self.state_root.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state_with_memory()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state_with_memory()
        if not isinstance(payload, dict):
            return self._empty_state_with_memory()
        state = self._empty_state()
        state.update(payload)
        state["shared_memory"] = self._shared_memory(state)
        return state

    def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = utc_now()
        state["shared_memory"] = self._shared_memory(state)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return state

    def save_portfolio(
        self,
        source_path: Path,
        holdings: list[dict[str, Any]],
        workbook_scan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state()
        state["portfolio"] = {
            "source_path": str(source_path),
            "file_name": source_path.name,
            "holding_count": len(holdings),
            "imported_at": utc_now(),
        }
        state["holdings"] = holdings
        state["workbook_scan"] = self._compact_workbook_scan(workbook_scan)
        state["workbook_scan_quality"] = self._workbook_scan_quality(workbook_scan)
        state["local_ai_product_status"] = None
        state["local_ai_summary"] = None
        state["local_ai_risk_warnings"] = []
        state["local_ai_command_result"] = None
        state["portfolio_memory"] = self._portfolio_memory(holdings)
        return self.save_state(state)

    def save_local_ai_result(
        self,
        product_status: dict[str, Any] | None,
        summary: dict[str, Any] | None,
        risk_warnings: list[dict[str, Any]] | None,
        command_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state = self.load_state()
        state["local_ai_product_status"] = (
            dict(product_status) if isinstance(product_status, dict) else None
        )
        state["local_ai_summary"] = dict(summary) if isinstance(summary, dict) else None
        state["local_ai_risk_warnings"] = list(risk_warnings or [])[:20]
        state["local_ai_command_result"] = self._compact_command_result(command_result)
        return self.save_state(state)

    def clear_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            self.state_path.unlink()
        return self._empty_state_with_memory()

    def add_ai_run(
        self,
        role: str,
        provider: str,
        prompt: str,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        run = {
            "run_id": uuid.uuid4().hex[:16],
            "role": role,
            "provider": provider,
            "prompt": prompt,
            "status": status,
            "content": content,
            "error": error,
            "created_at": utc_now(),
        }
        runs.insert(0, run)
        del runs[30:]
        self.save_state(state)
        return run

    def latest_ai_content(self, role: str) -> str:
        for run in self.load_state().get("ai_runs", []):
            if run.get("role") == role and run.get("content"):
                return str(run["content"])
        return ""

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "portfolio": None,
            "holdings": [],
            "workbook_scan": None,
            "workbook_scan_quality": None,
            "local_ai_product_status": None,
            "local_ai_summary": None,
            "local_ai_risk_warnings": [],
            "local_ai_command_result": None,
            "portfolio_memory": "",
            "shared_memory": "",
            "ai_runs": [],
            "updated_at": utc_now(),
        }

    def _empty_state_with_memory(self) -> dict[str, Any]:
        state = self._empty_state()
        state["shared_memory"] = self._shared_memory(state)
        return state

    @staticmethod
    def _portfolio_memory(holdings: list[dict[str, Any]]) -> str:
        lines = [
            "我的持股狀況：",
            "symbol | name | market | quantity | average_cost | currency",
        ]
        for holding in holdings:
            lines.append(
                " | ".join(
                    [
                        str(holding.get("symbol") or ""),
                        str(holding.get("name") or ""),
                        str(holding.get("market") or ""),
                        str(holding.get("quantity") or 0),
                        str(holding.get("average_cost") or ""),
                        str(holding.get("currency") or ""),
                    ]
                )
            )
        return "\n".join(lines)

    def _shared_memory(self, state: dict[str, Any]) -> str:
        lines = [
            "投資看盤共用記憶",
            f"updated_at: {state.get('updated_at') or utc_now()}",
            "",
            "Portfolio memory:",
            str(state.get("portfolio_memory") or "尚未匯入持股。"),
        ]
        product_status = state.get("local_ai_product_status")
        if isinstance(product_status, dict):
            lines.extend(
                [
                    "",
                    "Local AI product status:",
                    (
                        f"{product_status.get('state_label', '-')} "
                        f"score={product_status.get('score', '-')} "
                        f"mode={product_status.get('watch_status', '-')}"
                    ),
                    str(product_status.get("recommendation") or ""),
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
    def _shorten(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n...truncated..."

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

        score = InvestmentWatchRepository._int_value(selected_sheet.get("score"))
        valid_rows = InvestmentWatchRepository._int_value(
            selected_sheet.get("valid_data_row_count")
        )
        header_mode = str(selected_sheet.get("header_mode") or "")
        header_depth = InvestmentWatchRepository._int_value(
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

        if header_mode == "inferred":
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

    @staticmethod
    def _compact_command_result(
        command_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(command_result, dict):
            return None
        return {
            "intent": command_result.get("intent"),
            "sections": command_result.get("sections", []),
            "symbols": command_result.get("symbols", []),
            "matched_symbols": command_result.get("matched_symbols", []),
            "missing_symbols": command_result.get("missing_symbols", []),
            "portfolio_score": command_result.get("portfolio_score"),
            "portfolio_rating": command_result.get("portfolio_rating"),
            "risk_level": command_result.get("risk_level"),
            "next_actions": command_result.get("next_actions", []),
            "text": InvestmentWatchRepository._shorten(
                str(command_result.get("text") or ""),
                4000,
            ),
        }

    @staticmethod
    def _int_value(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
