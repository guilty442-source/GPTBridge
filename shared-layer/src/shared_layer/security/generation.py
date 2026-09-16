"""Security generation fence.

Credential or role changes raise ``security_generation``; sessions bound to
an older generation may still read but must not perform sensitive writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

SENSITIVE_WORKLOADS: Final[frozenset[str]] = frozenset(
    {"audit", "governance_write", "transport", "index", "rag_metadata", "reconcile"}
)

_READ_GENERATION_SQL: Final[str] = (
    "SELECT generation FROM gptbridge_security.security_generation ORDER BY generation DESC LIMIT 1"
)
_SET_GENERATION_SQL: Final[str] = "SELECT set_config('gptbridge.security_generation', %s, true)"


class SecurityGenerationError(RuntimeError):
    """Raised when a sensitive write is attempted under a stale generation."""


@dataclass
class GenerationLedger:
    """In-memory ledger used by tests and by the runtime cache."""

    current: int = 0
    history: list[tuple[int, str, float]] = field(default_factory=list)

    def raise_generation(self, reason: str, *, now: float) -> int:
        if not reason:
            raise ValueError("SECURITY_GENERATION_REASON_REQUIRED")
        self.current += 1
        self.history.append((self.current, reason, now))
        return self.current


def assert_generation_current(
    session_generation: int | str | None,
    workload: str,
    ledger: GenerationLedger,
) -> None:
    """Fail closed when a sensitive workload runs under a stale generation."""
    if workload not in SENSITIVE_WORKLOADS:
        return
    if session_generation is None or str(session_generation).strip() == "":
        raise SecurityGenerationError(
            f"SECURITY_GENERATION_UNBOUND:{workload}"
        )
    try:
        bound = int(session_generation)
    except (TypeError, ValueError) as error:
        raise SecurityGenerationError(f"SECURITY_GENERATION_INVALID:{session_generation}") from error
    if bound != ledger.current:
        raise SecurityGenerationError(
            f"SECURITY_GENERATION_STALE:session={bound}:current={ledger.current}:{workload}"
        )


def read_security_generation(connection) -> int:  # pragma: no cover - needs PG
    row = connection.execute(_READ_GENERATION_SQL).fetchone()
    return int(row[0]) if row else 0


def bind_security_generation(connection, generation: int) -> None:  # pragma: no cover - needs PG
    connection.execute(_SET_GENERATION_SQL, (str(generation),))


__all__ = [
    "SENSITIVE_WORKLOADS",
    "GenerationLedger",
    "SecurityGenerationError",
    "assert_generation_current",
    "bind_security_generation",
    "read_security_generation",
]
