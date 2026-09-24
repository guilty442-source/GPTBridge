"""mutual-fund contracts — identity, NAV, distribution, fee, transaction.

All money/NAV fields are ``decimal.Decimal`` — floats never represent
settlement-authoritative values. Every record carries source_id +
timestamps with timezone info. A fund's body (fund_id) and its share
classes (share_class_id) are distinct identities — different classes,
currencies, distribution policies and hedged variants never collapse
into one tradable instrument.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ..market.contracts import _dt, utcnow


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# ======================================================================
# Classification
# ======================================================================

class FundAssetClass(str, Enum):
    EQUITY = "equity"                    # 股票型
    BOND = "bond"                        # 債券型
    BALANCED = "balanced"                # 平衡型
    MULTI_ASSET = "multi_asset"          # 多重資產
    MONEY_MARKET = "money_market"        # 貨幣市場
    TARGET_DATE = "target_date"          # 目標日期
    INDEX = "index"                      # 指數型共同基金
    OTHER = "other"


class FundRegion(str, Enum):
    TAIWAN = "taiwan"
    US = "us"
    GLOBAL = "global"
    EMERGING = "emerging"
    SINGLE_COUNTRY = "single_country"
    SECTOR = "sector"                    # 特定產業
    THEMATIC = "thematic"                # 特定主題


@dataclass
class FundClassification:
    """Classification with provenance — never name-derived alone."""

    fund_id: str
    asset_class: str                     # FundAssetClass value
    region: str                          # FundRegion value
    sector: str = ""                     # when region == sector
    theme: str = ""                      # when region == thematic
    source_id: str = "operator"
    source_version: str = ""             # e.g. prospectus date/edition
    classified_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        self.classified_at = _dt(self.classified_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fund_id": self.fund_id,
            "asset_class": self.asset_class,
            "region": self.region,
            "sector": self.sector,
            "theme": self.theme,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "classified_at": self.classified_at.isoformat(),
        }


# ======================================================================
# Identity — fund body vs share class are distinct
# ======================================================================

class DistributionPolicy(str, Enum):
    ACCUMULATION = "accumulation"    # 累積型
    DISTRIBUTION = "distribution"    # 配息型
    BOTH = "both"


@dataclass
class FundIdentity:
    """Fund body + share class. instrument_id stays the unified
    ``fund:{fund_id}:{share_class}:{currency}`` form from the instrument
    contract — this identity is its authoritative backing record."""

    fund_id: str
    fund_name: str
    fund_company: str
    fund_type: str                       # FundAssetClass value
    share_class_id: str
    isin: str = ""
    domicile: str = ""
    base_currency: str = ""
    share_class_name: str = ""
    share_class_currency: str = ""
    distribution_policy: str = DistributionPolicy.ACCUMULATION.value
    hedged_currency: str = ""            # "" = unhedged
    inception_date: str = ""             # ISO date
    status: str = "active"               # active | suspended | liquidated

    @property
    def instrument_id(self) -> str:
        return (
            f"fund:{self.fund_id.upper()}:"
            f"{(self.share_class_id or 'NA').upper()}:"
            f"{(self.share_class_currency or self.base_currency or '').upper()}"
        )

    def same_fund(self, other: "FundIdentity") -> bool:
        return self.fund_id == other.fund_id

    def same_tradable(self, other: "FundIdentity") -> bool:
        return self.instrument_id == other.instrument_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "fund_id": self.fund_id,
            "fund_name": self.fund_name,
            "isin": self.isin,
            "fund_company": self.fund_company,
            "fund_type": self.fund_type,
            "domicile": self.domicile,
            "base_currency": self.base_currency,
            "share_class_id": self.share_class_id,
            "share_class_name": self.share_class_name,
            "share_class_currency": self.share_class_currency,
            "distribution_policy": self.distribution_policy,
            "hedged_currency": self.hedged_currency,
            "inception_date": self.inception_date,
            "status": self.status,
            "instrument_id": self.instrument_id,
        }


# ======================================================================
# NAV — published vs estimated vs confirmed are never interchangeable
# ======================================================================

class NavType(str, Enum):
    PUBLISHED = "published"    # official announced NAV
    ESTIMATED = "estimated"    # intraday estimate — never a deal price
    CONFIRMED = "confirmed"    # actual trade confirmation NAV


@dataclass
class FundNAV:
    fund_id: str
    share_class_id: str
    nav_date: date
    nav: Decimal
    currency: str
    source_id: str
    nav_type: str = NavType.PUBLISHED.value
    published_at: datetime | None = None
    received_at: datetime = field(default_factory=utcnow)
    revision: int = 1
    data_status: str = "ok"              # ok | corrected | stale

    def __post_init__(self) -> None:
        self.nav = Decimal(str(self.nav))
        self.nav_date = (
            date.fromisoformat(str(self.nav_date))
            if not isinstance(self.nav_date, date) else self.nav_date
        )
        if self.published_at is not None:
            self.published_at = _dt(self.published_at)
        self.received_at = _dt(self.received_at)

    @property
    def dedup_key(self) -> tuple[str, str, str, str]:
        return (self.fund_id, self.share_class_id,
                self.nav_date.isoformat(), self.nav_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fund_id": self.fund_id,
            "share_class_id": self.share_class_id,
            "nav_date": self.nav_date.isoformat(),
            "nav": str(self.nav),
            "currency": self.currency,
            "nav_type": self.nav_type,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "received_at": self.received_at.isoformat(),
            "source_id": self.source_id,
            "revision": self.revision,
            "data_status": self.data_status,
        }


# ======================================================================
# Distribution — income vs principal (return of capital) vs unconfirmed
# ======================================================================

class DistributionSource(str, Enum):
    INCOME = "income"                    # 收益分配
    PRINCIPAL = "principal"              # 已揭露本金分配 (return of capital)
    UNCONFIRMED = "unconfirmed"          # 來源未確認


@dataclass
class FundDistribution:
    share_class_id: str
    ex_distribution_date: date
    amount_per_unit: Decimal
    currency: str
    source_id: str
    fund_id: str = ""
    payment_date: date | None = None
    distribution_source: str = DistributionSource.UNCONFIRMED.value
    frequency: str = ""                  # monthly|quarterly|annual|irregular
    distribution_id: str = field(default_factory=lambda: _new_id("dist"))

    def __post_init__(self) -> None:
        self.amount_per_unit = Decimal(str(self.amount_per_unit))
        if not isinstance(self.ex_distribution_date, date):
            self.ex_distribution_date = date.fromisoformat(
                str(self.ex_distribution_date))
        if self.payment_date and not isinstance(self.payment_date, date):
            self.payment_date = date.fromisoformat(str(self.payment_date))

    def to_dict(self) -> dict[str, Any]:
        return {
            "distribution_id": self.distribution_id,
            "fund_id": self.fund_id,
            "share_class_id": self.share_class_id,
            "ex_distribution_date": self.ex_distribution_date.isoformat(),
            "payment_date": (
                self.payment_date.isoformat() if self.payment_date else None
            ),
            "amount_per_unit": str(self.amount_per_unit),
            "currency": self.currency,
            "distribution_source": self.distribution_source,
            "frequency": self.frequency,
            "source_id": self.source_id,
        }


# ======================================================================
# Fees — per fund/class/platform/date/holding-period/currency
# ======================================================================

class FeeKind(str, Enum):
    SUBSCRIPTION = "subscription"        # 申購手續費 (investor-paid)
    REDEMPTION = "redemption"            # 買回費用
    SHORT_TERM = "short_term"            # 短線交易費用
    MANAGEMENT = "management"            # 經理費 (NAV-embedded)
    CUSTODY = "custody"                  # 保管費 (NAV-embedded)
    PLATFORM = "platform"                # 平台費用
    FX = "fx"                            # 幣別轉換成本
    OTHER = "other"


class FeeCalc(str, Enum):
    FIXED = "fixed"                      # fixed amount
    PERCENT = "percent"                  # % of trade amount
    HOLDING_DAYS = "holding_days"        # applies only if held < min_days
    TIERED = "tiered"                    # discount brackets by amount


# Fee kinds already embedded in NAV — never deduct again on top.
NAV_EMBEDDED_FEES = frozenset(
    {FeeKind.MANAGEMENT.value, FeeKind.CUSTODY.value}
)


@dataclass
class FundFee:
    fund_id: str
    share_class_id: str
    kind: str                            # FeeKind value
    calc: str                            # FeeCalc value
    rate: Decimal = Decimal("0")         # percent (0.03 = 3%) or fixed amount
    currency: str = ""
    platform: str = ""                   # "" = applies to all platforms
    min_holding_days: int | None = None  # for short_term
    tiers: list[dict[str, Any]] = field(default_factory=list)  # tiered
    effective_from: str = ""             # ISO date
    effective_to: str = ""
    source_id: str = "operator"
    fee_id: str = field(default_factory=lambda: _new_id("fee"))

    @property
    def nav_embedded(self) -> bool:
        return self.kind in NAV_EMBEDDED_FEES

    def applies(
        self,
        platform: str,
        trade_date: date,
        holding_days: int | None = None,
        currency: str = "",
    ) -> bool:
        if self.platform and platform and self.platform != platform:
            return False
        if self.currency and currency and self.currency != currency:
            return False
        if self.effective_from and trade_date < date.fromisoformat(self.effective_from):
            return False
        if self.effective_to and trade_date > date.fromisoformat(self.effective_to):
            return False
        if self.calc == FeeCalc.HOLDING_DAYS.value:
            if holding_days is None or self.min_holding_days is None:
                return False
            if holding_days >= self.min_holding_days:
                return False
        return True

    def charge(self, amount: Decimal, holding_days: int | None = None) -> Decimal:
        amount = Decimal(str(amount))
        if self.calc == FeeCalc.FIXED.value:
            return self.rate
        if self.calc == FeeCalc.PERCENT.value:
            return amount * self.rate
        if self.calc == FeeCalc.HOLDING_DAYS.value:
            return amount * self.rate
        if self.calc == FeeCalc.TIERED.value:
            for tier in self.tiers:
                if amount <= Decimal(str(tier.get("up_to", "Infinity"))):
                    return amount * Decimal(str(tier["rate"]))
            return amount * self.rate
        return Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee_id": self.fee_id,
            "fund_id": self.fund_id,
            "share_class_id": self.share_class_id,
            "kind": self.kind,
            "calc": self.calc,
            "rate": str(self.rate),
            "currency": self.currency,
            "platform": self.platform,
            "min_holding_days": self.min_holding_days,
            "tiers": self.tiers,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "nav_embedded": self.nav_embedded,
            "source_id": self.source_id,
        }


# ======================================================================
# Transactions — explicit status machine, never submitted==filled
# ======================================================================

class FundTransactionType(str, Enum):
    SUBSCRIBE = "subscribe"              # 申購
    ADD = "add"                          # 加碼
    REDEEM = "redeem"                    # 買回
    PARTIAL_REDEEM = "partial_redeem"    # 部分買回
    SWITCH = "switch"                    # 基金轉換
    RECURRING = "recurring"              # 定期定額
    DISTRIBUTION = "distribution"        # 配息入帳
    REINVEST = "reinvest"                # 配息再投入


class FundTxnStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PRICING_PENDING = "PRICING_PENDING"
    PRICED = "PRICED"
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# Legal transitions — platform capability may further restrict these.
_TXN_TRANSITIONS: dict[str, frozenset[str]] = {
    FundTxnStatus.DRAFT.value: frozenset({
        FundTxnStatus.SUBMITTED.value, FundTxnStatus.CANCELLED.value}),
    FundTxnStatus.SUBMITTED.value: frozenset({
        FundTxnStatus.ACCEPTED.value, FundTxnStatus.REJECTED.value,
        FundTxnStatus.CANCELLED.value}),
    FundTxnStatus.ACCEPTED.value: frozenset({
        FundTxnStatus.PRICING_PENDING.value, FundTxnStatus.PRICED.value,
        FundTxnStatus.REJECTED.value}),
    FundTxnStatus.PRICING_PENDING.value: frozenset({
        FundTxnStatus.PRICED.value, FundTxnStatus.REJECTED.value}),
    FundTxnStatus.PRICED.value: frozenset({
        FundTxnStatus.SETTLEMENT_PENDING.value, FundTxnStatus.SETTLED.value}),
    FundTxnStatus.SETTLEMENT_PENDING.value: frozenset({
        FundTxnStatus.SETTLED.value}),
    FundTxnStatus.SETTLED.value: frozenset(),
    FundTxnStatus.REJECTED.value: frozenset(),
    FundTxnStatus.CANCELLED.value: frozenset(),
}


@dataclass
class FundTransaction:
    account_id: str
    fund_id: str
    share_class_id: str
    transaction_type: str                # FundTransactionType value
    amount: Decimal
    currency: str
    units: Decimal = Decimal("0")
    confirmed_nav: Decimal | None = None
    fees: Decimal = Decimal("0")
    status: str = FundTxnStatus.DRAFT.value
    application_date: date | None = None
    pricing_date: date | None = None
    confirmation_date: date | None = None
    settlement_date: date | None = None
    source_id: str = "operator"
    transaction_id: str = field(default_factory=lambda: _new_id("ftxn"))
    note: str = ""

    def __post_init__(self) -> None:
        self.amount = Decimal(str(self.amount))
        self.units = Decimal(str(self.units))
        self.fees = Decimal(str(self.fees))
        if self.confirmed_nav is not None:
            self.confirmed_nav = Decimal(str(self.confirmed_nav))
        for attr in ("application_date", "pricing_date",
                     "confirmation_date", "settlement_date"):
            v = getattr(self, attr)
            if v is not None and not isinstance(v, date):
                setattr(self, attr, date.fromisoformat(str(v)))

    @property
    def is_final(self) -> bool:
        return self.status in (
            FundTxnStatus.SETTLED.value,
            FundTxnStatus.REJECTED.value,
            FundTxnStatus.CANCELLED.value,
        )

    @property
    def credited(self) -> bool:
        """Only SETTLED transactions affect units/cash."""
        return self.status == FundTxnStatus.SETTLED.value

    def can_transition(self, target: str) -> bool:
        return target in _TXN_TRANSITIONS.get(self.status, frozenset())

    def to_dict(self) -> dict[str, Any]:
        def _d(v: date | None) -> str | None:
            return v.isoformat() if v else None
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "fund_id": self.fund_id,
            "share_class_id": self.share_class_id,
            "transaction_type": self.transaction_type,
            "amount": str(self.amount),
            "units": str(self.units),
            "confirmed_nav": (
                str(self.confirmed_nav) if self.confirmed_nav else None
            ),
            "currency": self.currency,
            "fees": str(self.fees),
            "status": self.status,
            "application_date": _d(self.application_date),
            "pricing_date": _d(self.pricing_date),
            "confirmation_date": _d(self.confirmation_date),
            "settlement_date": _d(self.settlement_date),
            "source_id": self.source_id,
            "note": self.note,
        }


# ======================================================================
# Holdings / exposure
# ======================================================================

@dataclass
class FundHolding:
    fund_id: str
    holding_id: str                      # instrument_id / isin / label
    name: str
    weight: Decimal                      # fraction of fund NAV (0.05 = 5%)
    asset_kind: str = "equity"           # equity|bond|cash|other
    country: str = ""
    sector: str = ""
    as_of_date: date | None = None       # disclosure date — shown, not hidden
    source_id: str = "operator"

    def __post_init__(self) -> None:
        self.weight = Decimal(str(self.weight))
        if self.as_of_date and not isinstance(self.as_of_date, date):
            self.as_of_date = date.fromisoformat(str(self.as_of_date))

    def to_dict(self) -> dict[str, Any]:
        return {
            "fund_id": self.fund_id,
            "holding_id": self.holding_id,
            "name": self.name,
            "weight": str(self.weight),
            "asset_kind": self.asset_kind,
            "country": self.country,
            "sector": self.sector,
            "as_of_date": (
                self.as_of_date.isoformat() if self.as_of_date else None
            ),
            "source_id": self.source_id,
        }


# ======================================================================
# Recommendation / strategy
# ======================================================================

class RecommendationType(str, Enum):
    SUBSCRIBE = "SUBSCRIBE"              # 申購
    ADD = "ADD"                          # 加碼
    HOLD = "HOLD"                        # 持有
    REDUCE = "REDUCE"                    # 減碼
    REDEEM = "REDEEM"                    # 贖回
    SWITCH = "SWITCH"                    # 轉換


@dataclass
class FundRecommendation:
    fund_id: str
    share_class_id: str
    account_id: str
    recommendation_type: str             # RecommendationType value
    analysis_date: date
    nav_date: date | None                # NAV basis — explicit, never "today"
    reasoning: str
    data_sources: list[str]
    investment_horizon: str = ""         # e.g. "3y+"
    risk_factors: list[str] = field(default_factory=list)
    suggested_amount: Decimal | None = None
    suggested_weight: Decimal | None = None
    reevaluate_when: list[str] = field(default_factory=list)
    model_id: str = ""
    model_version: str = ""
    strategy_version: str = ""
    recommendation_id: str = field(default_factory=lambda: _new_id("frec"))
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not isinstance(self.analysis_date, date):
            self.analysis_date = date.fromisoformat(str(self.analysis_date))
        if self.nav_date and not isinstance(self.nav_date, date):
            self.nav_date = date.fromisoformat(str(self.nav_date))
        self.created_at = _dt(self.created_at)
        if self.suggested_amount is not None:
            self.suggested_amount = Decimal(str(self.suggested_amount))
        if self.suggested_weight is not None:
            self.suggested_weight = Decimal(str(self.suggested_weight))

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "fund_id": self.fund_id,
            "share_class_id": self.share_class_id,
            "account_id": self.account_id,
            "recommendation_type": self.recommendation_type,
            "analysis_date": self.analysis_date.isoformat(),
            "nav_date": self.nav_date.isoformat() if self.nav_date else None,
            "investment_horizon": self.investment_horizon,
            "risk_factors": self.risk_factors,
            "reasoning": self.reasoning,
            "data_sources": self.data_sources,
            "suggested_amount": (
                str(self.suggested_amount) if self.suggested_amount else None
            ),
            "suggested_weight": (
                str(self.suggested_weight) if self.suggested_weight else None
            ),
            "reevaluate_when": self.reevaluate_when,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "strategy_version": self.strategy_version,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class FundStrategy:
    strategy_id: str
    strategy_version: str
    kind: str                            # allocation|recurring|rebalance|risk|switch|tracking
    parameters: dict[str, Any]
    status: str = "active"               # active | retired
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "kind": self.kind,
            "parameters": self.parameters,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }
