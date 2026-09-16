"""Credential rotation and emergency revocation.

Rotation order is fixed and non-destructive:

    create new -> verify new -> switch -> (grace period) -> revoke old

The old credential stays valid for ``grace_seconds`` so long-lived sessions
are not cut off; revocation happens only after the grace window closes.
Emergency revocation is a strict sequence:

    disable login -> terminate sessions -> rotate credential ->
    raise security generation -> audit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class RotationPhase(Enum):
    PLANNED = "planned"
    CREATED = "created"
    VERIFIED = "verified"
    SWITCHED = "switched"
    GRACE = "grace"
    REVOKED = "revoked"
    FAILED = "failed"


class RotationError(RuntimeError):
    """Raised when the rotation state machine is driven out of order."""


@dataclass
class CredentialVersion:
    version: int
    fingerprint: str
    created_at: float
    expires_at: float | None = None


@dataclass
class RotationPlan:
    role: str
    grace_seconds: float = 1800.0
    new_version: CredentialVersion | None = None
    old_version: CredentialVersion | None = None
    phase: RotationPhase = RotationPhase.PLANNED
    history: list[str] = field(default_factory=list)

    def _advance(self, phase: RotationPhase, note: str) -> None:
        self.phase = phase
        self.history.append(f"{phase.value}:{note}")

    def create_new(self, fingerprint: str, *, now: float) -> CredentialVersion:
        if self.phase is not RotationPhase.PLANNED:
            raise RotationError(f"ROTATION_OUT_OF_ORDER:{self.phase.value}")
        if not fingerprint:
            raise RotationError("ROTATION_FINGERPRINT_REQUIRED")
        version = (self.old_version.version + 1) if self.old_version else 1
        self.new_version = CredentialVersion(version=version, fingerprint=fingerprint, created_at=now)
        self._advance(RotationPhase.CREATED, f"v{version}")
        return self.new_version

    def verify_new(self, *, ok: bool) -> None:
        if self.phase is not RotationPhase.CREATED:
            raise RotationError(f"ROTATION_OUT_OF_ORDER:{self.phase.value}")
        if not ok:
            self._advance(RotationPhase.FAILED, "verify-failed")
            raise RotationError("ROTATION_VERIFY_FAILED")
        self._advance(RotationPhase.VERIFIED, "ok")

    def switch(self, *, now: float) -> float:
        if self.phase is not RotationPhase.VERIFIED:
            raise RotationError(f"ROTATION_OUT_OF_ORDER:{self.phase.value}")
        self._advance(RotationPhase.SWITCHED, f"v{self.new_version.version if self.new_version else 0}")
        self._advance(RotationPhase.GRACE, f"grace={self.grace_seconds}")
        return now + self.grace_seconds

    def grace_closed(self, *, now: float, grace_deadline: float) -> bool:
        return self.phase is RotationPhase.GRACE and now >= grace_deadline

    def revoke_old(self, *, now: float, grace_deadline: float) -> None:
        if self.old_version is None:
            raise RotationError("ROTATION_NO_OLD_CREDENTIAL")
        if not self.grace_closed(now=now, grace_deadline=grace_deadline):
            raise RotationError("ROTATION_GRACE_NOT_CLOSED")
        self._advance(RotationPhase.REVOKED, f"v{self.old_version.version}")


EMERGENCY_REVOKE_SEQUENCE: Final[tuple[str, ...]] = (
    "disable-login",
    "terminate-sessions",
    "rotate-credential",
    "raise-security-generation",
    "audit",
)


@dataclass
class EmergencyRevocation:
    role: str
    reason: str
    identity: str = ""
    steps: list[str] = field(default_factory=list)
    completed: bool = False

    def next_step(self) -> str | None:
        if self.completed:
            return None
        if len(self.steps) >= len(EMERGENCY_REVOKE_SEQUENCE):
            self.completed = True
            return None
        return EMERGENCY_REVOKE_SEQUENCE[len(self.steps)]

    def record(self, step: str) -> None:
        expected = self.next_step()
        if step != expected:
            raise RotationError(f"EMERGENCY_REVOKE_OUT_OF_ORDER:expected={expected}:got={step}")
        self.steps.append(step)
        if len(self.steps) == len(EMERGENCY_REVOKE_SEQUENCE):
            self.completed = True

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "reason": self.reason,
            "identity": self.identity,
            "steps": list(self.steps),
            "completed": self.completed,
        }


__all__ = [
    "EMERGENCY_REVOKE_SEQUENCE",
    "CredentialVersion",
    "EmergencyRevocation",
    "RotationError",
    "RotationPhase",
    "RotationPlan",
]
