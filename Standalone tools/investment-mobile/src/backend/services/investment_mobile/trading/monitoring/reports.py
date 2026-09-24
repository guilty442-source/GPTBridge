"""Daily / Weekly / Monthly investment reports — versioned, honest.

Every section is labelled by data class: confirmed market data /
confirmed account data / manual-imported / model analysis / simulated.
Missing data is marked as a gap, never filled with fabricated numbers.
Reports are versioned — history is never overwritten.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

REPORT_TYPES = ("daily", "weekly", "monthly")


class InvestmentReportService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "monitoring" / "reports"
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
    def generate(self, report_type: str, *, valuation: dict[str, Any],
                 positions: list[dict[str, Any]],
                 income: dict[str, Any] | None,
                 events: list[dict[str, Any]],
                 recommendations: list[dict[str, Any]],
                 allocation: dict[str, Any] | None = None,
                 risk: dict[str, Any] | None = None,
                 model_notes: str = "") -> dict[str, Any]:
        if report_type not in REPORT_TYPES:
            return {"ok": False, "error_code": "REPORT_TYPE_UNKNOWN"}
        gaps = []
        if not valuation.get("positions"):
            gaps.append("positions")
        if valuation.get("stale_data"):
            gaps.append("stale_market_data")
        if not recommendations:
            gaps.append("recommendations")
        day = int(time.time() // 86400)
        seq = sum(1 for r in self._reports
                  if r["report_type"] == report_type
                  and r["day"] == day) + 1
        report = {
            "report_id": f"rpt-{uuid.uuid4().hex[:10]}",
            "report_type": report_type,
            "day": day, "version": seq,
            "generated_at": time.time(),
            "sections": {
                "valuation": {**valuation, "data_class":
                              "confirmed-account + manual"},
                "positions": {"rows": positions,
                              "data_class": "manual/imported — "
                                            "broker_confirmed=false"},
                "income": income or {"gap": "no income data"},
                "events": {"rows": events[-50:],
                           "data_class": "monitoring"},
                "recommendations": {"rows": recommendations[-50:],
                                    "data_class": "model-analysis — "
                                                  "advisory only"},
                "allocation": allocation or {"gap": "no targets"},
                "risk": risk or {"gap": "insufficient history"},
                "model_notes": model_notes,
            },
            "data_gaps": gaps,
            "disclaimer": "模擬與手動資料已分開標示；未成交建議不等於"
                          "真實報酬；本報告非券商對帳結果",
        }
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False) + "\n")
        self._reports.append(report)
        return {"ok": True, "report": report}

    def list(self, report_type: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._reports
                if report_type is None or r["report_type"] == report_type]
        return rows[-limit:]

    def get(self, report_id: str) -> dict[str, Any] | None:
        for r in self._reports:
            if r["report_id"] == report_id:
                return r
        return None
