"""主權並行核心 — independent domain checks in parallel, dependent gates serial.

法典依據:
- CONCURRENCY: independent-domain-checks parallel-after-assignment +
  dependent-gates serial + result-join requires-all-mandatory-current proofs.
- A446: every tier receipted; the executor never self-declares success.
- A10/A11: fail-closed — one failed mandatory check denies the joined result.
- A69/A121: every run leaves a machine-readable receipt.

The core is intentionally bounded: each check runs under an optional
deadline, exceptions/timeouts are recorded as failed verdicts instead of
crashing the join, and a failed stage stops the dependent chain so no
downstream gate consumes stale evidence.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

AUTHORIZATION_GATE_DEADLINE_SECONDS: float = 5.0


@dataclass(frozen=True)
class DomainCheck:
    """One independent check executed inside a parallel stage."""

    name: str
    run: Callable[[], Any]
    mandatory: bool = True


@dataclass(frozen=True)
class CheckVerdict:
    """Machine-readable outcome of one check (A446 receipt fragment)."""

    name: str
    ok: bool
    duration_ms: int
    mandatory: bool = True
    error: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "name": self.name,
            "ok": self.ok,
            "mandatory": self.mandatory,
            "duration_ms": self.duration_ms,
        }
        if self.error:
            record["error"] = self.error
        if self.detail:
            record["detail"] = dict(self.detail)
        return record


@dataclass(frozen=True)
class ParallelRun:
    """Result of one parallel stage (result-join requires all proofs)."""

    label: str
    ok: bool
    verdicts: tuple[CheckVerdict, ...]
    failed: tuple[str, ...]

    def verdict(self, name: str) -> CheckVerdict | None:
        for item in self.verdicts:
            if item.name == name:
                return item
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "core": self.label,
            "mode": "parallel-independent-checks",
            "ok": self.ok,
            "failed": list(self.failed),
            "checks": [item.to_record() for item in self.verdicts],
        }


class SovereignParallelCore:
    """Run independent domain checks concurrently, joins fail closed.

    ``run_stage`` executes every check of one stage in parallel (input order
    is preserved in the receipt).  ``run_stages`` runs stages serially and
    stops the chain as soon as a mandatory check of a stage fails, so
    dependent gates never consume stale evidence (codex CONCURRENCY).
    """

    def __init__(self, *, label: str, deadline: float | None = None) -> None:
        self._label = label
        self._deadline = deadline

    async def run_stage(self, checks: Sequence[DomainCheck]) -> ParallelRun:
        verdicts = tuple(
            await asyncio.gather(*(self._execute(check) for check in checks))
        )
        failed = tuple(
            item.name
            for item in verdicts
            if item.mandatory and not item.ok
        )
        return ParallelRun(
            label=self._label,
            ok=not failed,
            verdicts=verdicts,
            failed=failed,
        )

    async def run_stages(
        self, stages: Sequence[Sequence[DomainCheck]]
    ) -> tuple[ParallelRun, ...]:
        runs: list[ParallelRun] = []
        for stage in stages:
            run = await self.run_stage(stage)
            runs.append(run)
            if not run.ok:
                break
        return tuple(runs)

    async def _execute(self, check: DomainCheck) -> CheckVerdict:
        started = time.perf_counter()
        ok = False
        error = ""
        detail: dict[str, Any] = {}
        try:
            result = check.run()
            if inspect.isawaitable(result):
                if self._deadline is not None:
                    result = await asyncio.wait_for(result, self._deadline)
                else:
                    result = await result
            if isinstance(result, bool):
                ok = result
            elif isinstance(result, Mapping):
                ok = bool(result.get("ok"))
                detail = dict(result)
            else:
                ok = bool(result)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "TimeoutError"
        except Exception as exc:  # fail-closed: record, never crash the join
            error = type(exc).__name__
        duration_ms = int((time.perf_counter() - started) * 1000)
        return CheckVerdict(
            name=check.name,
            ok=ok and not error,
            duration_ms=duration_ms,
            mandatory=check.mandatory,
            error=error,
            detail=detail,
        )


__all__ = [
    "AUTHORIZATION_GATE_DEADLINE_SECONDS",
    "CheckVerdict",
    "DomainCheck",
    "ParallelRun",
    "SovereignParallelCore",
]
