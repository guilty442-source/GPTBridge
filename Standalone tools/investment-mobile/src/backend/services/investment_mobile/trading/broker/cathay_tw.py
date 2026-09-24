"""國泰綜合證券 — Taiwan equities adapter (independent adapter boundary)."""

from __future__ import annotations

from .base import BrokerAdapter


class CathaySecuritiesAdapter(BrokerAdapter):
    broker_id = "cathay-tw"
    market = "tw"
    label = "國泰綜合證券"
