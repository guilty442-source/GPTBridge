"""Mutual-fund platform adapter — extensible multi-platform integration.

A single logical adapter fronts the ``fund`` market until specific fund
platform APIs are onboarded; additional platforms register their own
adapter instances and the registry routes by ``platform_id``.
"""

from __future__ import annotations

from .base import BrokerAdapter


class FundPlatformAdapter(BrokerAdapter):
    broker_id = "fund-platform"
    market = "fund"
    label = "基金平台（可擴充）"

    def __init__(self, state_dir, platform_id: str = "generic") -> None:
        self.platform_id = str(platform_id)
        self.broker_id = f"fund-platform-{self.platform_id}"
        super().__init__(state_dir)
