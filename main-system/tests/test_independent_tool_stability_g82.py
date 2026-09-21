"""G82 Xingcheng auto-loop arity regression."""
from __future__ import annotations

import asyncio

from governance.sovereigns.xingcheng.auto import XingchengAutoMixin


class _AutoHarness(XingchengAutoMixin):
    def __init__(self) -> None:
        self._pending_anomalies = []
        self._auto_metrics = {"anomalies_detected": 0, "last_anomaly": ""}
        self.notifications: list[int] = []

    async def _notify_anomalies(self) -> None:
        self.notifications.append(len(self._pending_anomalies))

    @staticmethod
    def _iso_now() -> str:
        return "2026-09-21T00:00:00Z"


def test_g82_process_anomalies_calls_zero_arity_notifier() -> None:
    harness = _AutoHarness()
    asyncio.run(
        harness._process_anomalies(
            [{"id": "a"}, {"id": "b"}],
            batch_size=1,
        )
    )
    assert harness.notifications == [1, 2]
    assert harness._auto_metrics["anomalies_detected"] == 2
