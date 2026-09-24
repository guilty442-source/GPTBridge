"""FundRecommendationService — 星澄 fund advice with provenance.

Every recommendation carries model identity, strategy version, data
sources, the NAV date it was based on and re-evaluation conditions —
full traceability. The service validates inputs before recording: an
AI recommendation must reference verifiable data (a recorded NAV date),
never fabricated performance or guaranteed returns.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .contracts import FundRecommendation, NavType, RecommendationType
from .nav import FundNAVService


class FundRecommendationService:
    def __init__(self, state_dir: Path, nav: FundNAVService) -> None:
        self._path = Path(state_dir) / "fund-recommendations.jsonl"
        self._nav = nav

    # ------------------------------------------------------------------
    def record(self, rec: FundRecommendation) -> dict[str, Any]:
        if rec.recommendation_type not in {
            t.value for t in RecommendationType
        }:
            return {"ok": False, "error_code": "RECOMMENDATION_TYPE_UNKNOWN"}
        # Every recommendation must name its NAV basis — never implicit.
        if rec.nav_date is None:
            return {"ok": False, "error_code": "NAV_BASIS_REQUIRED"}
        nav = self._nav.latest(
            rec.fund_id, rec.share_class_id,
            nav_type=NavType.PUBLISHED.value, as_of=rec.nav_date)
        if nav is None:
            return {"ok": False, "error_code": "NAV_BASIS_UNVERIFIABLE"}
        if not rec.data_sources:
            return {"ok": False, "error_code": "DATA_SOURCES_REQUIRED"}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        return {"ok": True, "recommendation": rec.to_dict(),
                "nav_basis": nav.to_dict()}

    def list(
        self, fund_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if fund_id and row.get("fund_id") != fund_id:
                continue
            out.append(row)
        return out[-int(limit):]
