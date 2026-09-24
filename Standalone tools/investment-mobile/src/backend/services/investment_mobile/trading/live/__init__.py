"""trading.live — formal trading core (LIVE path).

Phase-locked: ``LiveActivationGate.PHASE_LOCKED`` is True and
``LiveOrderManagementSystem.dispatch_enabled`` defaults False — real
broker dispatch cannot happen regardless of other conditions.
"""

from .activation import LiveActivationGate
from .audit_svc import TradingAuditService
from .authorization import TradingAuthorizationService
from .contracts import (
    AuditEvent, BROKER_FUNCTIONS, CapabilityStatus, EmergencyEvent,
    EmergencyScope, LiveExecution, LiveOrder, LiveOrderState,
    LiveRiskDecision, ReconciliationReport, ReconciliationResult,
    RiskVerdict, SubmissionRecord, TradingAuthorization,
    legal_transition, mask_sensitive,
)
from .core import LiveTradingCore
from .emergency import EmergencyTradingControl
from .failure import TradingFailureController
from .gateway import BrokerCredentialService, BrokerGateway
from .lifecycle import OrderLifecycle
from .orders import LiveOrderManagementSystem, OrderIdempotencyService
from .persistence import TradingPersistenceService
from .reconciliation import (
    AccountReconciliationService, LiveAccountService,
)
from .risk import (
    CapitalRiskController, CrossMarketRiskService, LiveRiskEngine,
    LossRiskController, PositionRiskController,
)

__all__ = [
    "AccountReconciliationService", "AuditEvent", "BROKER_FUNCTIONS",
    "BrokerCredentialService", "BrokerGateway", "CapitalRiskController",
    "CapabilityStatus", "CrossMarketRiskService", "EmergencyEvent",
    "EmergencyScope", "EmergencyTradingControl", "LiveAccountService",
    "LiveActivationGate", "LiveExecution", "LiveOrder",
    "LiveOrderManagementSystem", "LiveOrderState", "LiveRiskDecision",
    "LiveRiskEngine", "LiveTradingCore", "LossRiskController",
    "OrderIdempotencyService", "OrderLifecycle", "PositionRiskController",
    "ReconciliationReport", "ReconciliationResult", "RiskVerdict",
    "SubmissionRecord", "TradingAuditService", "TradingAuthorization",
    "TradingAuthorizationService", "TradingFailureController",
    "TradingPersistenceService", "legal_transition", "mask_sensitive",
]
