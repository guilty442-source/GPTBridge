"""FUBON_SUBBROKERAGE — 富邦證券美股複委託 adapter skeleton.

Awaiting the official sub-brokerage API verification; inert until then.
"""

from __future__ import annotations

from .base import BrokerAdapter


class FubonUsAdapter(BrokerAdapter):
    broker_id = "FUBON_SUBBROKERAGE"
    market = "us"
    label = "富邦證券複委託（美股）"
