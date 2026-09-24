"""MUTUAL_FUND_PROVIDER — generic fund platform adapter.

The fund platform is NOT yet decided — this adapter must never be
hardcoded to Cathay or Fubon. Until a provider API is selected and
verified, fund data enters through manual import
(``investment_fund_import``) and every dispatch denies.
"""

from __future__ import annotations

from .base import BrokerAdapter


class FundPlatformAdapter(BrokerAdapter):
    broker_id = "MUTUAL_FUND_PROVIDER"
    market = "fund"
    label = "共同基金平台（待接入）"
