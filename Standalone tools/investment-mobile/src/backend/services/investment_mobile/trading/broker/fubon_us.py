"""富邦證券複委託 — US equities adapter (independent adapter boundary)."""

from __future__ import annotations

from .base import BrokerAdapter


class FubonSubBrokerageAdapter(BrokerAdapter):
    broker_id = "fubon-us-sub"
    market = "us"
    label = "富邦證券複委託"
