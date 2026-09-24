"""live domain contracts — formal trading core vocabulary.

Distinct from ``trading.simulation`` (paper-* ids, simulated=True):
everything here describes REAL trading intent — but the phase lock
(``activation.LiveActivationGate.PHASE_LOCKED``) keeps dispatch disabled,
so these records are intents/decisions/evidence, never broker orders.

Shape-enforced rules:
- ``TradingAuthorization`` is issued only through
  ``TradingAuthorizationService.issue`` — AI/model issuers are refused.
- ``LiveOrder`` state changes only through ``OrderLifecycle.transition``;
  ``SUBMISSION_UNKNOWN`` forbids blind resubmission by contract.
- ``LiveRiskDecision.verdict`` is ALLOW / DENY / INCOMPLETE_EVIDENCE —
  deterministic rules only, never LLM discretion.
- Money fields are Decimal-as-text; no floats in live records.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ======================================================================
# Order lifecycle
# ======================================================================

class LiveOrderState(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    AUTHORIZED = "AUTHORIZED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"


# Legal transitions — anything not listed is refused.
_TRANSITIONS: dict[LiveOrderState, frozenset[LiveOrderState]] = {
    LiveOrderState.CREATED: frozenset({
        LiveOrderState.VALIDATING, LiveOrderState.REJECTED,
        LiveOrderState.CANCELLED}),
    LiveOrderState.VALIDATING: frozenset({
        LiveOrderState.AUTHORIZED, LiveOrderState.REJECTED,
        LiveOrderState.CANCELLED}),
    LiveOrderState.AUTHORIZED: frozenset({
        LiveOrderState.RISK_APPROVED, LiveOrderState.REJECTED,
        LiveOrderState.CANCELLED}),
    LiveOrderState.RISK_APPROVED: frozenset({
        LiveOrderState.SUBMISSION_PENDING, LiveOrderState.REJECTED,
        LiveOrderState.CANCELLED}),
    LiveOrderState.SUBMISSION_PENDING: frozenset({
        LiveOrderState.SUBMITTED, LiveOrderState.SUBMISSION_UNKNOWN,
        LiveOrderState.REJECTED, LiveOrderState.CANCELLED}),
    LiveOrderState.SUBMITTED: frozenset({
        LiveOrderState.ACKNOWLEDGED, LiveOrderState.SUBMISSION_UNKNOWN,
        LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED,
        LiveOrderState.CANCEL_PENDING, LiveOrderState.REJECTED,
        LiveOrderState.EXPIRED, LiveOrderState.RECONCILIATION_REQUIRED}),
    LiveOrderState.SUBMISSION_UNKNOWN: frozenset({
        LiveOrderState.SUBMITTED, LiveOrderState.ACKNOWLEDGED,
        LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED,
        LiveOrderState.REJECTED, LiveOrderState.RECONCILIATION_REQUIRED,
        LiveOrderState.CANCELLED}),
    LiveOrderState.ACKNOWLEDGED: frozenset({
        LiveOrderState.PARTIALLY_FILLED, LiveOrderState.FILLED,
        LiveOrderState.CANCEL_PENDING, LiveOrderState.REJECTED,
        LiveOrderState.EXPIRED, LiveOrderState.RECONCILIATION_REQUIRED}),
    LiveOrderState.PARTIALLY_FILLED: frozenset({
        LiveOrderState.FILLED, LiveOrderState.CANCEL_PENDING,
        LiveOrderState.CANCELLED, LiveOrderState.EXPIRED,
        LiveOrderState.RECONCILIATION_REQUIRED}),
    LiveOrderState.CANCEL_PENDING: frozenset({
        LiveOrderState.CANCELLED, LiveOrderState.PARTIALLY_FILLED,
        LiveOrderState.FILLED,  # cancel lost the race — fill stands
        LiveOrderState.RECONCILIATION_REQUIRED}),
    LiveOrderState.RECONCILIATION_REQUIRED: frozenset({
        LiveOrderState.ACKNOWLEDGED, LiveOrderState.PARTIALLY_FILLED,
        LiveOrderState.FILLED, LiveOrderState.CANCELLED,
        LiveOrderState.REJECTED, LiveOrderState.EXPIRED,
        LiveOrderState.SUBMITTED, LiveOrderState.SUBMISSION_UNKNOWN}),
    # terminal — no outbound edges
    LiveOrderState.FILLED: frozenset(),
    LiveOrderState.CANCELLED: frozenset(),
    LiveOrderState.REJECTED: frozenset(),
    LiveOrderState.EXPIRED: frozenset(),
}

_TERMINAL = frozenset({
    LiveOrderState.FILLED, LiveOrderState.CANCELLED,
    LiveOrderState.REJECTED, LiveOrderState.EXPIRED})


def legal_transition(src: LiveOrderState, dst: LiveOrderState) -> bool:
    return dst in _TRANSITIONS.get(src, frozenset())


def is_terminal(state: LiveOrderState) -> bool:
    return state in _TERMINAL


# ======================================================================
# Authorization
# ======================================================================

@dataclass
class TradingAuthorization:
    """Scoped trading grant — issued by the formal authority path only.

    Every scope dimension narrows the grant; empty list/None means the
    dimension is unconstrained by this grant (the other grants may still
    be required). ``expires_at`` is mandatory — no perpetual grants.
    """

    account_id: str
    user_id: str = ""
    market: str = ""                  # tw|us|fund — "" = grant-scope any
    broker_id: str = ""
    instrument_ids: list[str] = field(default_factory=list)  # [] = any
    strategy_ids: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=lambda: ["LIVE"])
    sides: list[str] = field(default_factory=lambda: ["buy", "sell"])
    max_order_notional: str = "0"     # Decimal text; "0" = no cap field
    max_daily_notional: str = "0"
    currency: str = ""
    issued_by: str = ""               # required — authority identity
    issued_at: float = field(default_factory=_now)
    expires_at: float = 0.0           # required > issued_at
    status: str = "ACTIVE"            # ACTIVE|REVOKED|EXPIRED
    authorization_id: str = field(
        default_factory=lambda: _new_id("authz"))
    scope_version: int = 1

    def expired(self, at: float | None = None) -> bool:
        return self.expires_at <= (at if at is not None else _now())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Risk
# ======================================================================

class RiskVerdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"


@dataclass
class LiveRiskDecision:
    """Deterministic risk verdict — INCOMPLETE_EVIDENCE never allows."""

    verdict: str                      # RiskVerdict value
    reason_codes: list[str] = field(default_factory=list)
    rule_version: str = "live-risk-v1"
    proposal_id: str = ""
    account_id: str = ""
    strategy_id: str = ""
    evaluated_at: float = field(default_factory=_now)
    decision_id: str = field(
        default_factory=lambda: _new_id("lrisk"))

    @property
    def allowed(self) -> bool:
        return self.verdict == RiskVerdict.ALLOW.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Orders / executions / submissions
# ======================================================================

@dataclass
class LiveOrder:
    """Formal order intent — persisted BEFORE any broker contact."""

    proposal_id: str
    account_id: str
    broker_id: str
    instrument_id: str
    side: str
    order_type: str                   # market|limit — capability-checked
    quantity: str                     # Decimal text
    limit_price: str = ""
    currency: str = ""
    market: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    client_order_key: str = ""        # idempotency key (required)
    internal_order_id: str = field(
        default_factory=lambda: _new_id("lord"))
    broker_order_id: str = ""
    state: str = LiveOrderState.CREATED.value
    filled_quantity: str = "0"
    avg_fill_price: str = ""
    authorization_id: str = ""
    risk_decision_id: str = ""
    expires_at: float = 0.0
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    simulated: bool = False           # live contract — always False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LiveExecution:
    """Broker-reported execution — never fabricated locally."""

    order_id: str
    account_id: str
    instrument_id: str
    side: str
    quantity: str
    price: str
    broker_execution_id: str = ""
    commission: str = "0"
    fees: dict[str, str] = field(default_factory=dict)
    currency: str = ""
    reported_at: float = field(default_factory=_now)
    execution_id: str = field(
        default_factory=lambda: _new_id("lexec"))
    simulated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubmissionRecord:
    """Every broker-contact attempt — append-only evidence."""

    order_id: str
    client_order_key: str
    account_id: str
    broker_id: str
    outcome: str                      # ack|reject|timeout|unknown|blocked
    broker_order_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    submitted_at: float = field(default_factory=_now)
    submission_id: str = field(
        default_factory=lambda: _new_id("lsub"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Broker capability
# ======================================================================

class CapabilityStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"               # never treated as supported


BROKER_FUNCTIONS = (
    "connect", "disconnect", "get_account", "get_balance",
    "get_positions", "get_orders", "get_executions",
    "place_order", "cancel_order", "modify_order",
)


# ======================================================================
# Reconciliation
# ======================================================================

class ReconciliationResult(str, Enum):
    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class ReconciliationReport:
    account_id: str
    result: str                       # ReconciliationResult value
    checked: list[str] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    local_revision: str = ""
    remote_revision: str = ""
    reconciled_at: float = field(default_factory=_now)
    report_id: str = field(
        default_factory=lambda: _new_id("recon"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Emergency
# ======================================================================

class EmergencyScope(str, Enum):
    ALL = "ALL"
    MARKET = "MARKET"
    ACCOUNT = "ACCOUNT"
    STRATEGY = "STRATEGY"


@dataclass
class EmergencyEvent:
    """Engage/release record — release requires an authorized actor."""

    scope: str                        # EmergencyScope value
    scope_key: str                    # market/account/strategy id or ""
    action: str                       # stop_new_orders|cancel_open|
                                      # liquidate|release
    by: str                           # authority identity — never AI
    reason: str = ""
    at: float = field(default_factory=_now)
    event_id: str = field(
        default_factory=lambda: _new_id("emg"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ======================================================================
# Audit
# ======================================================================

SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "credential", "certificate", "private_key", "session_key",
    "account_password", "pin", "otp",
})


def mask_sensitive(data: Any) -> Any:
    """Recursively mask sensitive fields — audit never stores secrets."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                out[k] = "***"
            else:
                out[k] = mask_sensitive(v)
        return out
    if isinstance(data, (list, tuple)):
        return [mask_sensitive(v) for v in data]
    return data


@dataclass
class AuditEvent:
    """Trading audit record — every stage emits one."""

    event_type: str
    correlation_id: str
    account_id: str = ""
    proposal_id: str = ""
    order_id: str = ""
    strategy_version: str = ""
    authorization_id: str = ""
    risk_decision_id: str = ""
    result: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=_now)
    event_id: str = field(
        default_factory=lambda: _new_id("levt"))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["detail"] = mask_sensitive(self.detail)
        return data
