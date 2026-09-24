"""LiveOrderManagementSystem + OrderIdempotencyService.

Submission pipeline (every hop journaled BEFORE the next external act):

    proposal → emergency/failure/recon/mode gates → idempotency
      → authorization → order CREATED→VALIDATING→AUTHORIZED
      → risk → RISK_APPROVED→SUBMISSION_PENDING
      → (dispatch_enabled && capability && verified) → broker
      → SUBMITTED/ACKNOWLEDGED | SUBMISSION_UNKNOWN | REJECTED

Idempotency: ``client_order_key`` is the dedup anchor — retries,
restarts, replays and duplicate model signals collapse onto one order.
``SUBMISSION_UNKNOWN`` never resubmits; ``resolve_unknown`` queries the
broker first. ``resubmit`` is legal only when reconciliation proves the
broker never received the order.
"""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..contracts import TradingMode
from ..modes import ModeGate
from .audit_svc import TradingAuditService
from .authorization import TradingAuthorizationService
from .contracts import (
    CapabilityStatus, LiveExecution, LiveOrder, LiveOrderState,
    SubmissionRecord, is_terminal,
)
from .emergency import EmergencyTradingControl
from .failure import TradingFailureController
from .lifecycle import OrderLifecycle
from .persistence import TradingPersistenceService
from .reconciliation import AccountReconciliationService
from .risk import LiveRiskEngine

_D = Decimal


def default_client_key(proposal: dict[str, Any]) -> str:
    raw = "|".join(str(proposal.get(k) or "") for k in (
        "proposal_id", "account_id", "instrument_id", "side",
        "quantity", "limit_price"))
    return "ck-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


class OrderIdempotencyService:
    def __init__(self, store: "LiveOrderManagementSystem") -> None:
        self._store = store

    def find(self, client_order_key: str) -> dict[str, Any] | None:
        order = self._store._by_key.get(str(client_order_key))
        return order.to_dict() if order else None

    def bind(self, client_order_key: str, order: LiveOrder) -> None:
        self._store._by_key[str(client_order_key)] = order


class LiveOrderManagementSystem:
    def __init__(self, state_dir: Path, *,
                 mode_gate: ModeGate,
                 persistence: TradingPersistenceService,
                 authorization: TradingAuthorizationService,
                 risk: LiveRiskEngine,
                 emergency: EmergencyTradingControl,
                 failure: TradingFailureController,
                 reconciliation: AccountReconciliationService,
                 audit: TradingAuditService,
                 gateway: Any,
                 dispatch_enabled: bool = False) -> None:
        self._gate = mode_gate
        self._store = persistence
        self._auth = authorization
        self._risk = risk
        self._emergency = emergency
        self._failure = failure
        self._recon = reconciliation
        self._audit = audit
        self._gateway = gateway
        self.dispatch_enabled = bool(dispatch_enabled)
        self._orders: dict[str, LiveOrder] = {}
        self._by_key: dict[str, LiveOrder] = {}
        self.lifecycle = OrderLifecycle(self._on_state)
        self.idempotency = OrderIdempotencyService(self)
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        for row in self._store.read("live_orders"):
            try:
                order = LiveOrder(**{
                    k: v for k, v in row.items()
                    if k in LiveOrder.__dataclass_fields__})
            except TypeError:
                continue
            self._orders[order.internal_order_id] = order
            if order.client_order_key:
                self._by_key[order.client_order_key] = order

    def _persist_order(self, order: LiveOrder) -> None:
        self._store.record("live_orders", order.to_dict())

    def _on_state(self, order: LiveOrder, to_state: str,
                  reason: str) -> None:
        self._store.record("live_order_events", {
            "order_id": order.internal_order_id,
            "to_state": to_state, "reason": reason,
            "at": time.time()})
        self._persist_order(order)

    def _go(self, order: LiveOrder, target: LiveOrderState,
            reason: str = "") -> dict[str, Any]:
        return self.lifecycle.transition(order, target, reason)

    # ------------------------------------------------------------------
    # submit
    # ------------------------------------------------------------------
    def submit(self, proposal: dict[str, Any]) -> dict[str, Any]:
        corr = str(proposal.get("proposal_id") or "")
        account_id = str(proposal.get("account_id") or "")
        market = str(proposal.get("market") or "")
        strategy_id = str(proposal.get("strategy_id") or "")

        # hard stops first — cheap, fail-closed
        if self._emergency.is_halted(market=market, account_id=account_id,
                                     strategy_id=strategy_id):
            return self._deny("EMERGENCY_HALTED", corr)
        if not self._failure.can_open_new():
            return self._deny("FAULT_ACTIVE", corr)
        if self._recon.halted(account_id):
            return self._deny("ACCOUNT_RECONCILIATION_REQUIRED", corr)
        if not self._gate.allows("execution"):
            return self._deny("MODE_BLOCKED", corr,
                              mode=self._gate.mode.value)

        # idempotency — collapse retries/duplicates onto the one order
        ckey = str(proposal.get("client_order_key") or
                   default_client_key(proposal))
        existing = self.idempotency.find(ckey)
        if existing is not None:
            self._audit.record("order.duplicate", correlation_id=corr,
                               order_id=existing["internal_order_id"],
                               result="dedup")
            return {"ok": True, "dedup": True, "order": existing}

        # authorization — scoped grant must cover every dimension
        notional = _D(str(proposal.get("notional") or
                        _D(str(proposal.get("quantity") or 0))
                        * _D(str(proposal.get("limit_price") or 0))))
        auth = self._auth.check(
            account_id=account_id, market=market,
            broker_id=str(proposal.get("broker_id") or ""),
            instrument_id=str(proposal.get("instrument_id") or ""),
            strategy_id=strategy_id,
            mode=self._gate.mode.value,
            side=str(proposal.get("side") or ""),
            notional=notional,
            user_id=str(proposal.get("user_id") or ""))
        if not auth.get("ok"):
            self._audit.record("order.auth_denied", correlation_id=corr,
                               account_id=account_id,
                               proposal_id=corr, result="DENY")
            return self._deny("NO_AUTHORIZATION", corr,
                              detail=auth.get("error_code"))
        authorization_id = auth["authorization"]["authorization_id"]

        # intent persisted BEFORE anything external — restart-safe
        order = LiveOrder(
            proposal_id=corr, account_id=account_id,
            broker_id=str(proposal.get("broker_id") or ""),
            instrument_id=str(proposal.get("instrument_id") or ""),
            side=str(proposal.get("side") or ""),
            order_type=str(proposal.get("order_type") or "market"),
            quantity=str(_D(str(proposal.get("quantity") or 0))),
            limit_price=str(proposal.get("limit_price") or ""),
            currency=str(proposal.get("currency") or ""),
            market=market, strategy_id=strategy_id,
            strategy_version=str(proposal.get("strategy_version") or ""),
            client_order_key=ckey,
            authorization_id=authorization_id,
            expires_at=float(proposal.get("expires_at") or 0))
        self._orders[order.internal_order_id] = order
        self.idempotency.bind(ckey, order)
        self._store.record("live_intents", {
            "client_order_key": ckey,
            "internal_order_id": order.internal_order_id,
            "proposal": dict(proposal), "at": time.time()})
        self._persist_order(order)
        self._audit.record("order.created", correlation_id=corr,
                           order_id=order.internal_order_id,
                           account_id=account_id,
                           authorization_id=authorization_id,
                           result="CREATED")

        for target in (LiveOrderState.VALIDATING,
                       LiveOrderState.AUTHORIZED):
            self._go(order, target)

        # risk — deterministic verdict; INCOMPLETE_EVIDENCE never allows
        ctx = dict(proposal.get("risk_ctx") or {})
        ctx.update({
            "proposal_id": corr, "account_id": account_id,
            "instrument_id": order.instrument_id, "side": order.side,
            "quantity": order.quantity, "notional": str(notional),
            "market": market, "strategy_id": strategy_id})
        decision = self._risk.evaluate(ctx)
        order.risk_decision_id = decision.decision_id
        self._store.record("live_risk_decisions", decision.to_dict())
        self._audit.record("risk.decided", correlation_id=corr,
                           order_id=order.internal_order_id,
                           risk_decision_id=decision.decision_id,
                           result=decision.verdict,
                           detail={"reason_codes": decision.reason_codes})
        if not decision.allowed:
            self._go(order, LiveOrderState.REJECTED, decision.verdict)
            return {"ok": False, "error_code": f"RISK_{decision.verdict}",
                    "order_id": order.internal_order_id,
                    "reason_codes": decision.reason_codes}
        self._go(order, LiveOrderState.RISK_APPROVED)
        self._go(order, LiveOrderState.SUBMISSION_PENDING)

        # dispatch — phase-locked by default
        if not self.dispatch_enabled:
            self._audit.record("order.dispatch_blocked",
                               correlation_id=corr,
                               order_id=order.internal_order_id,
                               result="LIVE_PHASE_LOCKED")
            return {"ok": False, "error_code": "LIVE_PHASE_LOCKED",
                    "order_id": order.internal_order_id,
                    "state": order.state}
        return self._dispatch(order)

    def _deny(self, code: str, corr: str, **extra: Any) -> dict[str, Any]:
        self._audit.record("order.denied", correlation_id=corr,
                           result=code, detail=dict(extra))
        return {"ok": False, "error_code": code, **extra}

    # ------------------------------------------------------------------
    # dispatch / reports
    # ------------------------------------------------------------------
    def _dispatch(self, order: LiveOrder) -> dict[str, Any]:
        cap = self._gateway.capability(order.broker_id, "place_order")
        if cap != CapabilityStatus.SUPPORTED.value:
            self._go(order, LiveOrderState.REJECTED,
                     f"CAPABILITY_{cap}")
            return self._deny(f"CAPABILITY_{cap}",
                              order.proposal_id,
                              order_id=order.internal_order_id)
        try:
            res = self._gateway.call(
                order.broker_id, "place_order",
                internal_order_id=order.internal_order_id,
                client_order_key=order.client_order_key,
                instrument_id=order.instrument_id,
                side=order.side, order_type=order.order_type,
                quantity=order.quantity, limit_price=order.limit_price)
        except Exception:
            res = {"ok": False, "error_code": "BROKER_CALL_FAILED"}
        outcome = ("ack" if res.get("ok") else
                   "reject" if res.get("error_code") == "BROKER_REJECTED"
                   else "unknown")
        self._store.record("live_submissions", SubmissionRecord(
            order_id=order.internal_order_id,
            client_order_key=order.client_order_key,
            account_id=order.account_id, broker_id=order.broker_id,
            outcome=outcome,
            broker_order_id=str(res.get("broker_order_id") or ""),
            detail={"error_code": res.get("error_code", "")}).to_dict())
        if res.get("ok"):
            order.broker_order_id = str(res.get("broker_order_id") or "")
            self._go(order, LiveOrderState.SUBMITTED, "broker_ack")
            return {"ok": True, "order_id": order.internal_order_id,
                    "broker_order_id": order.broker_order_id}
        if outcome == "reject":
            self._go(order, LiveOrderState.REJECTED,
                     str(res.get("rejection") or "broker"))
            return self._deny("BROKER_REJECTED", order.proposal_id,
                              order_id=order.internal_order_id,
                              detail=res.get("rejection", ""))
        # timeout/unknown — order may be live at the broker; NEVER resend
        self._go(order, LiveOrderState.SUBMISSION_UNKNOWN, outcome)
        return {"ok": False, "error_code": "SUBMISSION_UNKNOWN",
                "order_id": order.internal_order_id}

    def resolve_unknown(self, order_id: str) -> dict[str, Any]:
        """Query the broker BEFORE deciding anything — never blind-resend."""
        order = self._orders.get(str(order_id))
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if order.state != LiveOrderState.SUBMISSION_UNKNOWN.value:
            return {"ok": False, "error_code": "NOT_UNKNOWN_STATE"}
        res = self._gateway.call(order.broker_id, "get_orders")
        if not res.get("ok"):
            return {"ok": False, "error_code": "BROKER_QUERY_FAILED",
                    "order_id": order_id}
        remote = next((o for o in res.get("orders") or []
                       if str(o.get("client_order_key")) ==
                       order.client_order_key), None)
        if remote is None:
            # broker never received → safe to re-drive submission
            self._go(order, LiveOrderState.RECONCILIATION_REQUIRED,
                     "broker_no_order")
            return {"ok": True, "order_id": order_id,
                    "resolved": "not_received"}
        order.broker_order_id = str(remote.get("broker_order_id") or
                                    order.broker_order_id)
        state = str(remote.get("state") or "ACKNOWLEDGED")
        target = LiveOrderState.ACKNOWLEDGED
        if state in LiveOrderState.__members__:
            target = LiveOrderState(state)
        elif state == "PARTIALLY_FILLED":
            target = LiveOrderState.PARTIALLY_FILLED
        self._go(order, target, "broker_reconciled")
        return {"ok": True, "order_id": order_id,
                "resolved": state, "broker_order_id":
                order.broker_order_id}

    def resubmit(self, order_id: str) -> dict[str, Any]:
        """Resubmission is legal ONLY after reconcile proved non-receipt."""
        order = self._orders.get(str(order_id))
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        subs = [r for r in self._store.read("live_submissions")
                if r.get("order_id") == order.internal_order_id]
        if subs:
            last = subs[-1]
            if last.get("outcome") in ("timeout", "unknown"):
                return self._deny("RESUBMIT_FORBIDDEN_UNKNOWN",
                                  order.proposal_id,
                                  order_id=order_id)
        if order.state not in (LiveOrderState.SUBMISSION_PENDING.value,):
            return self._deny("RESUBMIT_ILLEGAL_STATE",
                              order.proposal_id, order_id=order_id,
                              state=order.state)
        return self._dispatch(order)

    # ------------------------------------------------------------------
    def record_report(self, report: dict[str, Any]) -> dict[str, Any]:
        """Broker report ingress — fills/cancels/rejects; never fabricated."""
        self._store.record("live_broker_reports", dict(report))
        order = self._orders.get(str(report.get("order_id") or ""))
        if order is None:
            key = str(report.get("client_order_key") or "")
            order = self._by_key.get(key)
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        rtype = str(report.get("type") or "fill")
        if rtype == "fill":
            qty = _D(str(report.get("quantity") or 0))
            price = _D(str(report.get("price") or 0))
            filled = _D(order.filled_quantity or "0") + qty
            if filled > _D(order.quantity):
                return self._deny("OVERFILL_REJECTED", order.proposal_id,
                                  order_id=order.internal_order_id)
            order.filled_quantity = str(filled)
            order.avg_fill_price = str(price)
            ex = LiveExecution(
                order_id=order.internal_order_id,
                account_id=order.account_id,
                instrument_id=order.instrument_id, side=order.side,
                quantity=str(qty), price=str(price),
                broker_execution_id=str(
                    report.get("broker_execution_id") or ""),
                commission=str(report.get("commission") or "0"),
                currency=order.currency)
            self._store.record("live_executions", ex.to_dict())
            target = (LiveOrderState.FILLED if filled >=
                      _D(order.quantity)
                      else LiveOrderState.PARTIALLY_FILLED)
            # cancel-pending lost the race — the fill stands
            self._go(order, target, "broker_fill")
            return {"ok": True, "order_id": order.internal_order_id,
                    "state": order.state, "filled": str(filled)}
        if rtype == "cancelled":
            return self._go(order, LiveOrderState.CANCELLED,
                            "broker_cancel")
        if rtype == "rejected":
            return self._go(order, LiveOrderState.REJECTED,
                            str(report.get("reason") or "broker"))
        return {"ok": False, "error_code": "REPORT_TYPE_UNKNOWN"}

    # ------------------------------------------------------------------
    def cancel(self, order_id: str, by: str = "operator"
               ) -> dict[str, Any]:
        order = self._orders.get(str(order_id))
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if is_terminal(LiveOrderState(order.state)):
            return {"ok": False, "error_code": "ORDER_TERMINAL"}
        if order.state == LiveOrderState.SUBMISSION_UNKNOWN.value:
            return {"ok": False, "error_code": "RECONCILE_FIRST"}
        res = self._go(order, LiveOrderState.CANCEL_PENDING,
                       f"by:{by}")
        if not res.get("ok"):
            return res
        if self.dispatch_enabled:
            out = self._gateway.call(
                order.broker_id, "cancel_order",
                broker_order_id=order.broker_order_id)
            if out.get("ok"):
                self._go(order, LiveOrderState.CANCELLED, "broker_ack")
                return {"ok": True, "order_id": order_id,
                        "state": order.state}
            if out.get("error_code") == "ALREADY_FILLED":
                self._go(order, LiveOrderState.FILLED, "cancel_lost_race")
                return {"ok": True, "order_id": order_id,
                        "state": order.state,
                        "note": "cancel raced fill — fill stands"}
            self._go(order,
                     LiveOrderState.RECONCILIATION_REQUIRED,
                     "cancel_uncertain")
            return {"ok": False, "error_code": "CANCEL_UNCERTAIN",
                    "order_id": order_id}
        return {"ok": True, "order_id": order_id,
                "state": order.state}

    def expire(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(str(order_id))
        if order is None:
            return {"ok": False, "error_code": "ORDER_NOT_FOUND"}
        if is_terminal(LiveOrderState(order.state)):
            return {"ok": False, "error_code": "ORDER_TERMINAL"}
        if order.expires_at and order.expires_at > time.time():
            return {"ok": False, "error_code": "NOT_EXPIRED"}
        return self._go(order, LiveOrderState.EXPIRED, "ttl")

    # ------------------------------------------------------------------
    def get(self, order_id: str) -> dict[str, Any] | None:
        o = self._orders.get(str(order_id))
        return o.to_dict() if o else None

    def open_orders(self) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self._orders.values()
                if not is_terminal(LiveOrderState(o.state))]

    def list(self) -> list[dict[str, Any]]:
        return [o.to_dict() for o in self._orders.values()]
