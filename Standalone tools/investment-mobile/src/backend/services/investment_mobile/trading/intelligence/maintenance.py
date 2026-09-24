"""Intelligence maintenance — governed, bounded, no resident scheduler."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .lifecycle import RecommendationLifecycle
from .router import ModelRouter
from .scheduler import InvestmentAnalysisScheduler


class IntelligenceMaintenance:
    """run_once: model health, expiry sweep, schedule status, degrade flags."""

    def __init__(self, router: ModelRouter,
                 lifecycle: RecommendationLifecycle,
                 scheduler: InvestmentAnalysisScheduler) -> None:
        self._router = router
        self._lifecycle = lifecycle
        self._scheduler = scheduler

    def run_once(self) -> dict[str, Any]:
        expired = self._lifecycle.expire_due()
        health = self._router.health()
        schedules = self._scheduler.list()
        pending = self._lifecycle.list(status="PUBLISHED")
        return {
            "ok": True,
            "at": datetime.now(timezone.utc).isoformat(),
            "model_available": health["available"],
            "model_degraded": not health["available"],
            "expired_recommendations": expired["expired"],
            "published_open": len(pending),
            "schedules": len(schedules),
            "inferences_recorded": health["inferences"],
            "note": "模型不可用僅降級分析；行情/持倉/風控不受影響",
        }
