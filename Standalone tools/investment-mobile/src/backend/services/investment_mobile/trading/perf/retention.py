"""InvestmentRetentionService — governed temp cleanup (§22).

Purges only derived/transient data: expired cache entries, finished
jobs, dedup tables, stale inference scratch. NEVER deletes holdings,
trade history, cash ledgers, strategy versions, audit, risk decisions,
or recovery evidence — those are authoritative and exempt by policy.
"""
from __future__ import annotations

import time
from typing import Any, Callable

# Categories this service is allowed to touch.
PURGABLE = ("expired_cache", "finished_jobs", "dedup_index",
            "inference_scratch", "download_scratch")


class InvestmentRetentionService:
    def __init__(self, *, purgers: dict[str, Callable[[], int]] | None = None
                 ) -> None:
        self._purgers = purgers or {}
        self._runs: list[dict[str, Any]] = []
        # hard-blocked categories — kept for the audit trail
        self._blocked_attempts = 0

    def purge(self, category: str) -> dict[str, Any]:
        cat = str(category)
        if cat not in PURGABLE:
            self._blocked_attempts += 1
            return {"ok": False, "error_code": "RETENTION_PROTECTED",
                    "category": cat,
                    "note": "正式持倉/交易/審計/風控/策略版本禁止自動刪除"}
        fn = self._purgers.get(cat)
        removed = fn() if fn else 0
        run = {"category": cat, "removed": int(removed or 0),
               "at": time.time()}
        self._runs.append(run)
        return {"ok": True, **run}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "purgable": list(PURGABLE),
                "blocked_attempts": self._blocked_attempts,
                "recent": list(self._runs[-10:]),
                "note": "清理僅限暫存/快取——正式紀錄受保護"}
