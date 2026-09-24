"""Strategy contracts — StrategyDefinition, status machine, types.

LIVE_ELIGIBLE means *validation passed* only — real trading still
requires the independent authorization path (ModeGate + broker
api_verified). Strategies never hold execution capability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StrategyType:
    TREND_FOLLOWING = "TREND_FOLLOWING"      # 趨勢追蹤
    MEAN_REVERSION = "MEAN_REVERSION"        # 均值回歸
    MOMENTUM = "MOMENTUM"                    # 動能策略
    BREAKOUT = "BREAKOUT"                    # 突破策略
    MULTI_FACTOR = "MULTI_FACTOR"            # 多因子策略
    ASSET_ALLOCATION = "ASSET_ALLOCATION"    # 資產配置
    PORTFOLIO_REBALANCING = "PORTFOLIO_REBALANCING"  # 再平衡
    FUND_RECURRING_INVESTMENT = "FUND_RECURRING_INVESTMENT"  # 定期定額
    ALL = frozenset({
        TREND_FOLLOWING, MEAN_REVERSION, MOMENTUM, BREAKOUT,
        MULTI_FACTOR, ASSET_ALLOCATION, PORTFOLIO_REBALANCING,
        FUND_RECURRING_INVESTMENT,
    })


class StrategyStatus:
    DRAFT = "DRAFT"
    TESTING = "TESTING"
    VALIDATED = "VALIDATED"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"   # validation passed — NOT authorization
    RETIRED = "RETIRED"
    ALL = frozenset({
        DRAFT, TESTING, VALIDATED, SHADOW, PAPER, LIVE_ELIGIBLE, RETIRED,
    })


_ALLOWED: dict[str, frozenset[str]] = {
    StrategyStatus.DRAFT: frozenset({StrategyStatus.TESTING,
                                     StrategyStatus.RETIRED}),
    StrategyStatus.TESTING: frozenset({StrategyStatus.VALIDATED,
                                       StrategyStatus.DRAFT,
                                       StrategyStatus.RETIRED}),
    StrategyStatus.VALIDATED: frozenset({StrategyStatus.SHADOW,
                                         StrategyStatus.TESTING,
                                         StrategyStatus.RETIRED}),
    StrategyStatus.SHADOW: frozenset({StrategyStatus.PAPER,
                                      StrategyStatus.VALIDATED,
                                      StrategyStatus.RETIRED}),
    StrategyStatus.PAPER: frozenset({StrategyStatus.LIVE_ELIGIBLE,
                                     StrategyStatus.SHADOW,
                                     StrategyStatus.RETIRED}),
    StrategyStatus.LIVE_ELIGIBLE: frozenset({StrategyStatus.PAPER,
                                            StrategyStatus.RETIRED}),
    StrategyStatus.RETIRED: frozenset(),
}


class MarketKind:
    TAIWAN_EQUITY = "TAIWAN_EQUITY"
    US_EQUITY = "US_EQUITY"
    MUTUAL_FUND = "MUTUAL_FUND"
    ALL = frozenset({TAIWAN_EQUITY, US_EQUITY, MUTUAL_FUND})


@dataclass
class StrategyDefinition:
    strategy_id: str
    strategy_name: str
    strategy_type: str               # StrategyType
    market: str                      # MarketKind
    instrument_scope: list[str] = field(default_factory=list)
    timeframe: str = "1d"
    parameters: dict[str, Any] = field(default_factory=dict)
    risk_profile: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    status: str = StrategyStatus.DRAFT
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.strategy_id:
            errs.append("STRATEGY_ID_REQUIRED")
        if self.strategy_type not in StrategyType.ALL:
            errs.append("STRATEGY_TYPE_UNKNOWN")
        if self.market not in MarketKind.ALL:
            errs.append("MARKET_UNKNOWN")
        if self.status not in StrategyStatus.ALL:
            errs.append("STATUS_UNKNOWN")
        # mutual-fund strategies must not assume equity realtime fills
        if (self.market == MarketKind.MUTUAL_FUND
                and self.parameters.get("execution_model") == "realtime"):
            errs.append("FUND_REALTIME_EXECUTION_FORBIDDEN")
        return errs

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "strategy_type": self.strategy_type,
            "market": self.market,
            "instrument_scope": list(self.instrument_scope),
            "timeframe": self.timeframe,
            "parameters": dict(self.parameters),
            "risk_profile": dict(self.risk_profile),
            "version": self.version, "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "StrategyDefinition":
        return StrategyDefinition(
            strategy_id=str(d.get("strategy_id") or ""),
            strategy_name=str(d.get("strategy_name") or ""),
            strategy_type=str(d.get("strategy_type") or ""),
            market=str(d.get("market") or ""),
            instrument_scope=list(d.get("instrument_scope") or []),
            timeframe=str(d.get("timeframe") or "1d"),
            parameters=dict(d.get("parameters") or {}),
            risk_profile=dict(d.get("risk_profile") or {}),
            version=int(d.get("version") or 1),
            status=str(d.get("status") or StrategyStatus.DRAFT),
            created_at=_dt(d.get("created_at")) or _now(),
            updated_at=_dt(d.get("updated_at")) or _now(),
        )


@dataclass
class StrategyVersionSnapshot:
    """Immutable snapshot — a version is never overwritten."""

    strategy_id: str
    version: int
    definition: dict[str, Any]       # StrategyDefinition.to_dict()
    code_version: str = ""
    data_version: str = ""
    model_version: str = ""
    risk_params: dict[str, Any] = field(default_factory=dict)
    cost_assumptions: dict[str, Any] = field(default_factory=dict)
    backtest_run_ids: list[str] = field(default_factory=list)
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    snapshot_id: str = field(default_factory=lambda: _new_id("sver"))
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "strategy_id": self.strategy_id, "version": self.version,
            "definition": self.definition,
            "code_version": self.code_version,
            "data_version": self.data_version,
            "model_version": self.model_version,
            "risk_params": self.risk_params,
            "cost_assumptions": self.cost_assumptions,
            "backtest_run_ids": list(self.backtest_run_ids),
            "validation_results": self.validation_results,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ParamSearchRecord:
    """One parameter-search evaluation — overfitting audit trail."""

    strategy_id: str
    version: int
    parameters: dict[str, Any]
    data_window: str
    split: str                        # in_sample|validation|out_of_sample|walk_forward
    metrics: dict[str, Any] = field(default_factory=dict)
    search_id: str = field(default_factory=lambda: _new_id("ps"))
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "search_id": self.search_id,
            "strategy_id": self.strategy_id, "version": self.version,
            "parameters": self.parameters, "data_window": self.data_window,
            "split": self.split, "metrics": self.metrics,
            "created_at": self.created_at.isoformat(),
        }


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
