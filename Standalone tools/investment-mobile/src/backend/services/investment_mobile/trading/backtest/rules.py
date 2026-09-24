"""Market rule profiles + broker capability.

Rules are config per market/instrument/effective date — no global
assumption that all TW stocks share identical trading conditions.
BrokerCapabilityProfile restricts simulated order types to what the
real channel actually allows (Fubon sub-brokerage ≠ US-native broker).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class MarketRules:
    market: str
    lot_size: int = 1
    odd_lot_allowed: bool = True
    limit_pct: Decimal | None = None       # daily price limit (None = none)
    settlement_days: int = 2
    min_commission: Decimal = Decimal("0")
    extended_hours: bool = False


# 台股：±10% 漲跌幅、整張 1000 股（零股另標）、T+2 交割
TAIWAN_RULES = MarketRules(
    market="TAIWAN_EQUITY", lot_size=1000, odd_lot_allowed=True,
    limit_pct=Decimal("0.10"), settlement_days=2, min_commission=Decimal("20"))

# 美股：無漲跌幅、零股依券商、複委託不支援盤前後
US_RULES = MarketRules(
    market="US_EQUITY", lot_size=1, odd_lot_allowed=True,
    limit_pct=None, settlement_days=1, extended_hours=False)


@dataclass
class BrokerCapabilityProfile:
    """What the *actual* trading channel supports — simulation boundary."""

    broker_id: str
    market: str
    order_types: list[str] = field(default_factory=lambda: ["market"])
    extended_hours: bool = False
    fractional_shares: bool = False
    short_selling: bool = False
    margin: bool = False
    min_order_value: Decimal = Decimal("0")
    notes: str = ""

    def allows(self, order_type: str) -> bool:
        return order_type in self.order_types


CAPABILITY_PROFILES: dict[str, BrokerCapabilityProfile] = {
    "CATHAY_SECURITIES": BrokerCapabilityProfile(
        broker_id="CATHAY_SECURITIES", market="TAIWAN_EQUITY",
        order_types=["market", "limit"], extended_hours=False,
        short_selling=False, min_order_value=Decimal("0"),
        notes="盤中零股另行確認；漲跌幅 ±10%"),
    "FUBON_SUBBROKERAGE": BrokerCapabilityProfile(
        broker_id="FUBON_SUBBROKERAGE", market="US_EQUITY",
        order_types=["market", "limit"], extended_hours=False,
        fractional_shares=False, short_selling=False, margin=False,
        min_order_value=Decimal("0"),
        notes="複委託：不支援盤前盤後/放空/融資/碎股"),
    "MUTUAL_FUND_PROVIDER": BrokerCapabilityProfile(
        broker_id="MUTUAL_FUND_PROVIDER", market="MUTUAL_FUND",
        order_types=["nav_order"],
        notes="以公告淨值計價；申請截止後以次一淨值"),
}


def rules_for(market: str, instrument_kind: str = "stock") -> MarketRules:
    if market == "US_EQUITY":
        return US_RULES
    return TAIWAN_RULES


def capability_for(broker_id: str) -> BrokerCapabilityProfile | None:
    return CAPABILITY_PROFILES.get(broker_id)
