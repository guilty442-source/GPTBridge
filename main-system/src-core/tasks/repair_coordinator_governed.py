"""Governed repair request mixin (A185 split).

Contains the request_governed_repair method extracted from
RepairCoordinator.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from core_system.auto_repair_chain import (
    HealthSignal,
    HealthState,
)

from .repair_coordinator_types import _iso_now
from .repair_coordinator_governed_helpers import (
    _execute_crash_repair,
)


class RepairGovernedMixin:
    """Governed repair request entry point."""

    project_root: Any
    _orchestrator: Any

    def try_acquire(self, *, failure_code: str, owner: str) -> bool:
        raise NotImplementedError

    def release(self, *, owner: str, failure_code: str) -> None:
        raise NotImplementedError

    def _read_requests(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _write_requests(self, requests: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def request_governed_repair(
        self,
        failure_code: str,
        owner: str,
        *,
        decision_proof: dict[str, Any],
        repair_executor: Any | None = None,
        signal_only: bool = False,
    ) -> dict[str, Any]:
        """A154 governed repair entry — records decision proof before mutation."""
        report: dict[str, Any] = {
            "governed": True,
            "failure_code": failure_code,
            "owner": owner,
            "signal_only": signal_only or repair_executor is None,
            "ok": False,
        }

        # FORBID:duplicate-repair-owner — acquire lock first.
        if not self.try_acquire(failure_code=failure_code, owner=owner):
            report["reason"] = "duplicate-repair-owner; another owner holds the lock"
            report["decision_proof"] = decision_proof
            return report

        request_id = uuid4().hex
        request_record: dict[str, Any] = {
            "request_id": request_id,
            "failure_code": failure_code,
            "owner": owner,
            "decision_proof": decision_proof,
            "requested_at": _iso_now(),
            "status": "pending",
            "signal_only": signal_only or repair_executor is None,
        }

        # Use the governance-compliant auto-repair chain (A261)
        if self._orchestrator is not None:
            signal = HealthSignal(
                component_id=decision_proof.get("component_id", "unknown"),
                dimension=decision_proof.get("dimension", "runtime-readiness"),
                state=HealthState(decision_proof.get("state", "critical")),
                severity=decision_proof.get("severity", 5),
                evidence={
                    "failure_code": failure_code,
                    "owner": owner,
                    "decision_proof": decision_proof,
                    **decision_proof.get("evidence", {}),
                },
            )
            chain_result = self._orchestrator.process_health_signal(signal, actor=owner)
            report.update(chain_result)
            report["request_id"] = request_id
            report["ok"] = chain_result.get("stage") == "complete"
            self.release(owner=owner, failure_code=failure_code)
            return report

        # Fallback: signal-only path for crash repair when orchestrator unavailable
        if signal_only or repair_executor is None:
            requests = self._read_requests()
            requests.append(request_record)
            self._write_requests(requests)
            report["request_id"] = request_id
            report["ok"] = True
            report["reason"] = "signal written to information layer; awaiting decision-sovereign decision"
            self.release(owner=owner, failure_code=failure_code)
            return report

        # Crash repair: backend is dead, decision-sovereign unavailable.
        # Governor directive (2026-09-18): the user-confirmation gate is
        # retired — governed crash repair proceeds under the system-audit
        # flow without waiting for a user switch.

        _execute_crash_repair(
            request_record, request_id, repair_executor, report,
            self._read_requests, self._write_requests,
        )
        self.release(owner=owner, failure_code=failure_code)
        return report


__all__ = ["RepairGovernedMixin"]
