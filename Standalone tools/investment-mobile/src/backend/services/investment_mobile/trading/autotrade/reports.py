"""AutonomousTradingReport — daily/weekly/monthly strategy reports.

Every section is labelled BACKTEST | SHADOW | PAPER — simulated results
are never presented as real-account returns. Missing data is an explicit
gap, never filled with fabricated numbers.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

REPORT_TYPES = ("daily", "weekly", "monthly")


class AutonomousTradingReport:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "autotrade" / "reports"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "reports.jsonl"
        self._reports: list[dict[str, Any]] = []
        self._replay()

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                self._reports.append(json.loads(line))
            except Exception:
                continue

    # ------------------------------------------------------------------
    def generate(self, report_type: str, *, runs: list[dict[str, Any]],
                 performance: list[dict[str, Any]],
                 shadow_signals: list[dict[str, Any]],
                 paper_orders: list[dict[str, Any]],
                 risk_events: list[dict[str, Any]],
                 improvements: list[dict[str, Any]],
                 model_notes: str = "") -> dict[str, Any]:
        if report_type not in REPORT_TYPES:
            return {"ok": False, "error_code": "REPORT_TYPE_UNKNOWN"}
        gaps = []
        if not runs:
            gaps.append("strategies")
        if not performance:
            gaps.append("performance")
        day = int(time.time() // 86400)
        seq = sum(1 for r in self._reports
                  if r["report_type"] == report_type
                  and r["day"] == day) + 1
        report = {
            "report_id": f"atr-{uuid.uuid4().hex[:10]}",
            "report_type": report_type, "day": day, "version": seq,
            "generated_at": time.time(),
            "sections": {
                "tw_strategies": {"rows": [r for r in runs
                                           if r.get("market") == "tw"],
                                  "data_class": "SIMULATED"},
                "us_strategies": {"rows": [r for r in runs
                                           if r.get("market") == "us"],
                                  "data_class": "SIMULATED"},
                "fund_research": {"rows": [r for r in runs
                                           if r.get("market") == "fund"],
                                  "data_class": "SIMULATED"},
                "performance": {"rows": performance,
                                "data_class": "PAPER — 模擬"},
                "shadow_signals": {"rows": shadow_signals[-50:],
                                   "data_class": "SHADOW — 訊號記錄"},
                "paper_orders": {"rows": paper_orders[-50:],
                                 "data_class": "PAPER — 模擬成交"},
                "risk_events": {"rows": risk_events[-50:],
                                "data_class": "monitoring"},
                "improvements": {"rows": improvements[-20:],
                                 "data_class": "model-analysis — 草稿"},
                "model_notes": model_notes,
            },
            "data_gaps": gaps,
            "disclaimer": "BACKTEST/SHADOW/PAPER 皆為模擬結果——"
                          "不構成真實帳戶收益；策略研究建議非已完成升級",
        }
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False) + "\n")
        self._reports.append(report)
        return {"ok": True, "report": report}

    def list(self, report_type: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._reports
                if report_type is None
                or r["report_type"] == report_type]
        return rows[-limit:]
