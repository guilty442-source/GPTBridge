"""星澄 AI 投資決策中心 — data contracts.

Every stage of the analysis pipeline carries typed objects; free-form
text never transports formal trading parameters. All monetary values
use Decimal; all timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EvidenceKind:
    VERIFIED_FACT = "VERIFIED_FACT"
    CALCULATED_RESULT = "CALCULATED_RESULT"
    MODEL_INTERPRETATION = "MODEL_INTERPRETATION"
    UNVERIFIED_INFORMATION = "UNVERIFIED_INFORMATION"
    ALL = frozenset({
        VERIFIED_FACT, CALCULATED_RESULT,
        MODEL_INTERPRETATION, UNVERIFIED_INFORMATION,
    })


class RecommendationType:
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    ADD = "ADD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    REBALANCE = "REBALANCE"
    SUBSCRIBE = "SUBSCRIBE"      # fund only
    REDEEM = "REDEEM"            # fund only
    SWITCH = "SWITCH"            # fund only
    ALL = frozenset({
        BUY, SELL, HOLD, ADD, REDUCE, EXIT, REBALANCE,
        SUBSCRIBE, REDEEM, SWITCH,
    })
    FUND_ONLY = frozenset({SUBSCRIBE, REDEEM, SWITCH})


class RecommendationStatus:
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    ARCHIVED = "ARCHIVED"
    ALL = frozenset({
        CREATED, VALIDATED, PUBLISHED, ACKNOWLEDGED,
        EXPIRED, INVALIDATED, ARCHIVED,
    })


class ProposalStatus:
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    ALL = frozenset({DRAFT, VALIDATED, SUBMITTED, REJECTED, EXPIRED, CONSUMED})


class OutcomeKind:
    AI_ANALYSIS = "ai_analysis"       # recommendation follow-up only
    SIMULATED = "simulated"           # paper/shadow trading
    REAL = "real"                     # executed trades — never mixed
    ALL = frozenset({AI_ANALYSIS, SIMULATED, REAL})


class AnalysisTaskKind:
    QUICK_MARKET = "quick_market"         # 快速行情分析
    DEEP_RESEARCH = "deep_research"       # 深度投資研究
    EARNINGS = "earnings"                 # 財報分析
    FUND = "fund"                         # 基金分析
    PORTFOLIO_RISK = "portfolio_risk"     # 持倉風險分析
    REPORT = "report"                     # 投資報告生成
    ALL = frozenset({
        QUICK_MARKET, DEEP_RESEARCH, EARNINGS,
        FUND, PORTFOLIO_RISK, REPORT,
    })


@dataclass
class AnalysisEvidence:
    """A single sourced fact/result feeding an analysis."""

    kind: str                            # EvidenceKind
    claim: str                           # what is asserted
    value: Any = None                    # structured payload (optional)
    source_id: str = ""                  # e.g. tdcc, polygon, filing-xyz
    data_timestamp: str = ""             # when the underlying data was produced
    data_version: str = ""
    computation: str = ""                # deterministic method name if calculated
    evidence_id: str = field(default_factory=lambda: _new_id("ev"))
    created_at: datetime = field(default_factory=_now)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.kind not in EvidenceKind.ALL:
            errors.append("EVIDENCE_KIND_UNKNOWN")
        if not self.claim:
            errors.append("EVIDENCE_CLAIM_REQUIRED")
        if self.kind == EvidenceKind.VERIFIED_FACT and not self.source_id:
            errors.append("VERIFIED_FACT_REQUIRES_SOURCE")
        if self.kind == EvidenceKind.CALCULATED_RESULT and not self.computation:
            errors.append("CALCULATED_REQUIRES_METHOD")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id, "kind": self.kind,
            "claim": self.claim, "value": self.value,
            "source_id": self.source_id,
            "data_timestamp": self.data_timestamp,
            "data_version": self.data_version,
            "computation": self.computation,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class AnalysisRun:
    """One execution of the analysis pipeline — auditable end to end."""

    task_kind: str
    instrument_id: str = ""
    market: str = ""
    account_id: str = ""
    model_id: str = ""
    model_version: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    data_window: str = ""
    stages_completed: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    evidence: list[AnalysisEvidence] = field(default_factory=list)
    findings: dict[str, Any] = field(default_factory=dict)
    missing_data: list[str] = field(default_factory=list)
    degraded: bool = False
    run_id: str = field(default_factory=lambda: _new_id("run"))
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "task_kind": self.task_kind,
            "instrument_id": self.instrument_id, "market": self.market,
            "account_id": self.account_id, "model_id": self.model_id,
            "model_version": self.model_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "data_window": self.data_window,
            "stages_completed": list(self.stages_completed),
            "stages_skipped": list(self.stages_skipped),
            "evidence": [e.to_dict() for e in self.evidence],
            "findings": self.findings,
            "missing_data": list(self.missing_data),
            "degraded": self.degraded,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat()
            if self.finished_at else None,
        }


@dataclass
class InvestmentRecommendation:
    """Advisory output for the user — never executable itself."""

    account_id: str
    instrument_id: str
    instrument_type: str                  # stock|etf|fund|index
    market: str
    recommendation_type: str              # RecommendationType
    reasoning: str = ""
    risk_factors: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    reference_price: Decimal | None = None
    reference_nav: Decimal | None = None
    suggested_weight: Decimal | None = None
    observation_window: str = ""
    trigger_conditions: list[str] = field(default_factory=list)
    reevaluate_conditions: list[str] = field(default_factory=list)
    analysis_timestamp: datetime = field(default_factory=_now)
    market_data_timestamp: datetime | None = None
    model_id: str = ""
    model_version: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    data_quality: str = "complete"        # complete|partial|degraded
    status: str = RecommendationStatus.CREATED
    recommendation_id: str = field(default_factory=lambda: _new_id("rec"))
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.recommendation_type not in RecommendationType.ALL:
            errors.append("REC_TYPE_UNKNOWN")
        if (self.recommendation_type in RecommendationType.FUND_ONLY
                and self.instrument_type != "fund"):
            errors.append("FUND_ONLY_REC_ON_NON_FUND")
        if not self.instrument_id:
            errors.append("INSTRUMENT_REQUIRED")
        if not self.model_id:
            errors.append("MODEL_ID_REQUIRED")
        if not self.evidence_refs:
            errors.append("EVIDENCE_REQUIRED")
        if self.instrument_type != "fund" and self.suggested_weight is not None:
            # sizing may be present but must come from risk math upstream
            if self.suggested_weight < 0 or self.suggested_weight > 1:
                errors.append("WEIGHT_OUT_OF_RANGE")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "account_id": self.account_id,
            "instrument_id": self.instrument_id,
            "instrument_type": self.instrument_type,
            "market": self.market,
            "recommendation_type": self.recommendation_type,
            "reasoning": self.reasoning,
            "risk_factors": list(self.risk_factors),
            "evidence_refs": list(self.evidence_refs),
            "reference_price": str(self.reference_price)
            if self.reference_price is not None else None,
            "reference_nav": str(self.reference_nav)
            if self.reference_nav is not None else None,
            "suggested_weight": str(self.suggested_weight)
            if self.suggested_weight is not None else None,
            "observation_window": self.observation_window,
            "trigger_conditions": list(self.trigger_conditions),
            "reevaluate_conditions": list(self.reevaluate_conditions),
            "analysis_timestamp": self.analysis_timestamp.isoformat(),
            "market_data_timestamp": self.market_data_timestamp.isoformat()
            if self.market_data_timestamp else None,
            "model_id": self.model_id, "model_version": self.model_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "data_quality": self.data_quality,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat()
            if self.expires_at else None,
        }


@dataclass
class RecommendationVersion:
    """Immutable historical version of a recommendation."""

    recommendation_id: str
    version: int
    snapshot: dict[str, Any]
    change_reason: str = ""
    changed_by: str = ""
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "version": self.version, "snapshot": self.snapshot,
            "change_reason": self.change_reason,
            "changed_by": self.changed_by,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class RecommendationOutcome:
    """Post-hoc evaluation — AI/simulated/real tracked separately."""

    recommendation_id: str
    outcome_kind: str                      # OutcomeKind
    horizon_days: int
    price_at_recommendation: Decimal | None = None
    price_at_evaluation: Decimal | None = None
    return_pct: Decimal | None = None
    max_favorable_pct: Decimal | None = None
    max_adverse_pct: Decimal | None = None
    signal_valid: bool | None = None
    conditions_held: bool | None = None
    evaluated_at: datetime = field(default_factory=_now)
    outcome_id: str = field(default_factory=lambda: _new_id("out"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "recommendation_id": self.recommendation_id,
            "outcome_kind": self.outcome_kind,
            "horizon_days": self.horizon_days,
            "price_at_recommendation": str(self.price_at_recommendation)
            if self.price_at_recommendation is not None else None,
            "price_at_evaluation": str(self.price_at_evaluation)
            if self.price_at_evaluation is not None else None,
            "return_pct": str(self.return_pct)
            if self.return_pct is not None else None,
            "max_favorable_pct": str(self.max_favorable_pct)
            if self.max_favorable_pct is not None else None,
            "max_adverse_pct": str(self.max_adverse_pct)
            if self.max_adverse_pct is not None else None,
            "signal_valid": self.signal_valid,
            "conditions_held": self.conditions_held,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


@dataclass
class AnalysisSchedule:
    """Calendar-driven scheduled analysis slot."""

    schedule_id: str
    market: str                            # tw|us|fund|portfolio
    slot: str                              # pre_open|intraday|post_close|nav_update|daily|weekly|monthly
    enabled: bool = True
    last_run_at: datetime | None = None
    last_market_date: str = ""             # guards stale-data reruns
    next_due: datetime | None = None
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id, "market": self.market,
            "slot": self.slot, "enabled": self.enabled,
            "last_run_at": self.last_run_at.isoformat()
            if self.last_run_at else None,
            "last_market_date": self.last_market_date,
            "next_due": self.next_due.isoformat() if self.next_due else None,
            "run_count": self.run_count,
        }


@dataclass
class ModelAnalysisRecord:
    """One model inference invocation — provenance for every AI output."""

    run_id: str
    task_kind: str
    model_id: str
    model_version: str = ""
    prompt_hash: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    status: str = "ok"                     # ok|degraded|failed|skipped
    created_at: datetime = field(default_factory=_now)
    record_id: str = field(default_factory=lambda: _new_id("minf"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id, "run_id": self.run_id,
            "task_kind": self.task_kind, "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_hash": self.prompt_hash,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms, "status": self.status,
            "created_at": self.created_at.isoformat(),
        }
