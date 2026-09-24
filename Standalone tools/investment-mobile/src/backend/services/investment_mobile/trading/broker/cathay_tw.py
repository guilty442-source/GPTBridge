"""CATHAY_SECURITIES — 國泰證券台股 adapter skeleton.

Awaiting the official trading API documentation and verification flow;
until ``api_verified`` the adapter denies every dispatch (fail closed).
"""

from __future__ import annotations

from .base import BrokerAdapter


class CathayTwAdapter(BrokerAdapter):
    broker_id = "CATHAY_SECURITIES"
    market = "tw"
    label = "國泰綜合證券（台股）"
