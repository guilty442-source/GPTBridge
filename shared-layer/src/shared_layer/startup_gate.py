"""Startup readiness ladder and the write-readiness contract.

Startup converges through ten explicit phases:

    BOOTSTRAP -> GOVERNANCE_VALIDATED -> SECURITY_VALIDATED ->
    DATABASE_FOUNDATION_READY -> CENTRAL_AUTHORITY_READY ->
    MODULE_PRIVATE_READY -> SEMANTIC_INDEX_READY -> RECOVERY_READY ->
    READ_MODEL_READY -> CORE_READY

Formal writes require ``authority_ready AND security_ready AND audit_ready``;
a bare ``SELECT 1`` never counts as PostgreSQL ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .health_states import ComponentHealth, HealthState, ReasonCode


class StartupPhase(Enum):
    BOOTSTRAP = "BOOTSTRAP"
    GOVERNANCE_VALIDATED = "GOVERNANCE_VALIDATED"
    SECURITY_VALIDATED = "SECURITY_VALIDATED"
    DATABASE_FOUNDATION_READY = "DATABASE_FOUNDATION_READY"
    CENTRAL_AUTHORITY_READY = "CENTRAL_AUTHORITY_READY"
    MODULE_PRIVATE_READY = "MODULE_PRIVATE_READY"
    SEMANTIC_INDEX_READY = "SEMANTIC_INDEX_READY"
    RECOVERY_READY = "RECOVERY_READY"
    READ_MODEL_READY = "READ_MODEL_READY"
    CORE_READY = "CORE_READY"


PHASE_ORDER: Final[tuple[StartupPhase, ...]] = tuple(StartupPhase)

_MANDATORY_PHASES: Final[frozenset[StartupPhase]] = frozenset(
    {
        StartupPhase.BOOTSTRAP,
        StartupPhase.GOVERNANCE_VALIDATED,
        StartupPhase.SECURITY_VALIDATED,
        StartupPhase.DATABASE_FOUNDATION_READY,
        StartupPhase.CENTRAL_AUTHORITY_READY,
        StartupPhase.RECOVERY_READY,
    }
)


class StartupGateError(RuntimeError):
    """Raised when startup advances without its prerequisites."""


@dataclass
class PhaseEvidence:
    phase: StartupPhase
    ready: bool
    health: ComponentHealth | None = None
    detail: str = ""


@dataclass
class StartupGate:
    """Tracks the ten-phase ladder; phases advance strictly in order."""

    reached: list[StartupPhase] = field(default_factory=list)
    evidence: dict[StartupPhase, PhaseEvidence] = field(default_factory=dict)

    @property
    def current(self) -> StartupPhase:
        return self.reached[-1] if self.reached else StartupPhase.BOOTSTRAP

    def next_phase(self) -> StartupPhase | None:
        index = PHASE_ORDER.index(self.current)
        if index + 1 >= len(PHASE_ORDER):
            return None
        return PHASE_ORDER[index + 1]

    def advance(self, phase: StartupPhase, *, ready: bool, health: ComponentHealth | None = None, detail: str = "") -> None:
        if not self.reached and phase is not StartupPhase.BOOTSTRAP:
            raise StartupGateError(f"STARTUP_MUST_BEGIN_AT_BOOTSTRAP:got={phase.value}")
        expected = self.next_phase()
        if self.reached and expected is not None and phase is not expected:
            raise StartupGateError(f"STARTUP_PHASE_OUT_OF_ORDER:expected={expected.value}:got={phase.value}")
        if not ready:
            if phase in _MANDATORY_PHASES:
                raise StartupGateError(f"STARTUP_PHASE_NOT_READY:{phase.value}")
            raise StartupGateError(f"STARTUP_OPTIONAL_PHASE_REJECTED:{phase.value}")
        self.reached.append(phase)
        self.evidence[phase] = PhaseEvidence(phase, ready, health, detail)

    def ready(self) -> bool:
        return self.current is StartupPhase.CORE_READY

    def failed_components(self) -> list[ComponentHealth]:
        return [
            item.health
            for item in self.evidence.values()
            if item.health is not None and item.health.state is not HealthState.HEALTHY
        ]


@dataclass
class WriteReadiness:
    authority_ready: bool
    security_ready: bool
    audit_ready: bool
    reasons: list[ReasonCode] = field(default_factory=list)

    def writable(self) -> bool:
        return self.authority_ready and self.security_ready and self.audit_ready

    def assert_writable(self) -> None:
        if not self.writable():
            missing = [
                name
                for name, value in (
                    ("authority", self.authority_ready),
                    ("security", self.security_ready),
                    ("audit", self.audit_ready),
                )
                if not value
            ]
            raise StartupGateError("FORMAL_WRITE_REQUIRES:" + ",".join(missing))

    def as_dict(self) -> dict[str, object]:
        return {
            "authority_ready": self.authority_ready,
            "security_ready": self.security_ready,
            "audit_ready": self.audit_ready,
            "writable": self.writable(),
            "reason_codes": [reason.value for reason in self.reasons],
        }


__all__ = [
    "PHASE_ORDER",
    "PhaseEvidence",
    "StartupGate",
    "StartupGateError",
    "StartupPhase",
    "WriteReadiness",
]
