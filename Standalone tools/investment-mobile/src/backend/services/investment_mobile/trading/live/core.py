"""LiveTradingCore — facade composing the formal trading layer.

Pipeline (no stage skippable):

    星澄 proposal → authorization → risk → OMS → gateway → adapter
      → broker → report → reconciliation → audit

The core NEVER holds broker credentials itself (gateway owns them) and
``dispatch_enabled`` defaults False — production wiring keeps it off
this phase; tests enable it only with ``MockBrokerAdapter``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..broker.base import BrokerRegistry
from ..modes import ModeGate
from .activation import LiveActivationGate
from .audit_svc import TradingAuditService
from .authorization import TradingAuthorizationService
from .emergency import EmergencyTradingControl
from .failure import TradingFailureController
from .gateway import BrokerCredentialService, BrokerGateway
from .orders import LiveOrderManagementSystem
from .persistence import TradingPersistenceService
from .reconciliation import (
    AccountReconciliationService, LiveAccountService,
)
from .risk import LiveRiskEngine


class LiveTradingCore:
    def __init__(self, state_dir: Path, *,
                 mode_gate: ModeGate,
                 accounts: Any = None,
                 risk_engine: Any = None,
                 gateway: BrokerGateway | None = None,
                 dispatch_enabled: bool = False) -> None:
        self._dir = Path(state_dir)
        self.persistence = TradingPersistenceService(state_dir)
        self.credentials = BrokerCredentialService(state_dir)
        if gateway is None:
            registry = BrokerRegistry(state_dir)
            gateway = BrokerGateway(registry._adapters, self.credentials)
        self.gateway = gateway
        self.authorization = TradingAuthorizationService(
            state_dir, self.persistence_journal)
        self.risk = LiveRiskEngine(state_dir)
        self.emergency = EmergencyTradingControl(self.persistence_journal)
        self.failure = TradingFailureController(
            state_dir, self.persistence_journal)
        self.reconciliation = AccountReconciliationService(
            state_dir, self.persistence_journal)
        self.live_accounts = LiveAccountService(self.persistence.record)
        self.audit = TradingAuditService(self.persistence.record)
        self.gate = LiveActivationGate(
            state_dir, mode_gate=mode_gate, accounts=accounts,
            gateway=self.gateway, risk_engine=risk_engine)
        self.orders = LiveOrderManagementSystem(
            state_dir, mode_gate=mode_gate,
            persistence=self.persistence,
            authorization=self.authorization, risk=self.risk,
            emergency=self.emergency, failure=self.failure,
            reconciliation=self.reconciliation, audit=self.audit,
            gateway=self.gateway, dispatch_enabled=dispatch_enabled)

    # ------------------------------------------------------------------
    def persistence_journal(self, kind: str,
                            row: dict[str, Any] | None) -> Any:
        """Journal adapter — ``row=None`` reads, else appends."""
        if row is None:
            return self.persistence.read(kind)
        self.persistence.record(kind, row)
        return {"ok": True}

    # ------------------------------------------------------------------
    def submit(self, proposal: dict[str, Any]) -> dict[str, Any]:
        return self.orders.submit(proposal)

    def status(self) -> dict[str, Any]:
        return {
            "phase_locked": self.gate.PHASE_LOCKED,
            "dispatch_enabled": self.orders.dispatch_enabled,
            "readiness": self.gate.readiness(),
            "emergency": self.emergency.status(),
            "failure": self.failure.status(),
            "open_orders": len(self.orders.open_orders()),
        }

    def recover(self) -> dict[str, Any]:
        """Boot reconciliation — report only, never auto-resubmit."""
        state = self.persistence.recover()
        return {"ok": True, **state,
                "policy": "reconcile_first_no_blind_resubmit"}

    def close(self) -> None:
        self.persistence.close()
