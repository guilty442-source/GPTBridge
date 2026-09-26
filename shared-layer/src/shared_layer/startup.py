from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

# This startup probe covers the bounded local degraded store. Canonical shared
# structured data remains PostgreSQL and must be checked by the host bootstrap.
from .local.database import DatabaseHealthCheck, DatabaseSettings
from .performance.thread_budget import bounded_workers

STATE_READY = "READY"
STATE_DEGRADED = "DEGRADED"
STATE_FAILED = "FAILED"
STATE_RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class GateResult:
    component: str
    critical: bool
    passed: bool
    fault_code: str
    message: str
    data: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "critical": self.critical,
            "passed": self.passed,
            "status": (
                "READY"
                if self.passed
                else ("FAILED" if self.critical else "RECOVERING")
            ),
            "fault_code": self.fault_code,
            "message": self.message,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class StartupReport:
    ready: bool
    stages: tuple[str, ...]
    database: dict[str, object]
    vectord: dict[str, object]
    state: str = STATE_READY
    ollama: dict[str, object] = field(default_factory=dict)
    gates: tuple[dict[str, object], ...] = field(default_factory=tuple)


def _gate_data_from_database_health(database: dict[str, object]) -> dict[str, object]:
    return {
        "database": database.get("database"),
        "migration_count": database.get("migration_count"),
        "latency_ms": database.get("latency_ms"),
    }


class SharedLayerStartup:
    """Probe bounded local degraded storage and optional vectord/Ollama health.

    Canonical PostgreSQL readiness is enforced by the host bootstrap before
    this local recovery surface may be treated as normal operation.
    """

    def __init__(
        self,
        shared_root: Path | str,
        settings: DatabaseSettings,
        vectord_health: Callable[[], dict[str, object]],
        registry_check: Callable[[], bool],
        locator_check: Callable[[], bool],
        governance_check: Callable[[], bool],
        ollama_health: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.root = Path(shared_root).resolve()
        self.settings = settings
        self.vectord_health = vectord_health
        self.registry_check = registry_check
        self.locator_check = locator_check
        self.governance_check = governance_check
        self.ollama_health = ollama_health

    def _safe_check(self, name: str, critical: bool, call: Callable[[], bool]) -> GateResult:
        try:
            ok = bool(call() is True)
        except Exception as exc:
            return GateResult(
                name,
                critical,
                False,
                f"{name.upper().replace('-', '_')}_GATE_ERROR",
                str(exc)[:300],
            )
        return GateResult(
            name,
            critical,
            ok,
            f"{name.upper().replace('-', '_')}_READY" if ok else f"{name.upper().replace('-', '_')}_UNAVAILABLE",
            "ready" if ok else "gate not satisfied",
        )

    def run(self) -> StartupReport:
        gates: list[GateResult] = []
        stages: list[str] = []

        database = asdict(DatabaseHealthCheck(self.settings).run())
        database_gate = GateResult(
            "database-health",
            True,
            bool(database.get("available")),
            str(database.get("fault_code") or "LOCAL_POSTGRES_UNAVAILABLE"),
            str(database.get("message") or database.get("error") or "ready"),
            _gate_data_from_database_health(database),
        )
        gates.append(database_gate)
        if not database_gate.passed:
            return self._build_report(gates, stages, database, {}, {})
        stages.extend(("local-database-health", "database-health"))

        # S4: vectord and ollama are independent non-critical probes — run them
        # concurrently. Hard gates below stay sequential to preserve the
        # critical early-exit report shape.
        vectord: dict = {}
        ollama: dict = {}

        def _probe_vectord() -> dict:
            try:
                return dict(self.vectord_health() or {})
            except Exception as exc:
                return {"available": False, "last_error": str(exc)[:300]}

        def _probe_ollama() -> dict:
            if self.ollama_health is None:
                return {}
            try:
                return dict(self.ollama_health() or {})
            except Exception as exc:
                return {"available": False, "last_error": str(exc)[:300]}

        with ThreadPoolExecutor(max_workers=bounded_workers(2)) as pool:
            vectord = pool.submit(_probe_vectord).result()
            ollama = pool.submit(_probe_ollama).result()

        vectord_available = vectord.get("available") is True
        vectord_gate = GateResult(
            "vectord",
            False,
            vectord_available,
            "VECTORD_READY" if vectord_available else "VECTORD_UNAVAILABLE",
            "ready" if vectord_available else str(vectord.get("last_error") or "vectord unavailable"),
            {"available": vectord_available},
        )
        gates.append(vectord_gate)
        if vectord_available:
            stages.append("vectord-health-collection")
        else:
            stages.append("vectord-degraded")

        if self.ollama_health is not None:
            ollama_available = ollama.get("available") is True
            ollama_gate = GateResult(
                "ollama",
                False,
                ollama_available,
                "OLLAMA_READY" if ollama_available else "OLLAMA_UNAVAILABLE",
                "ready" if ollama_available else str(ollama.get("last_error") or "ollama unavailable"),
                {"available": ollama_available},
            )
            gates.append(ollama_gate)
            stages.append("ollama-health" if ollama_available else "ollama-recovering")

        hard_gates = (
            ("registry", self.registry_check),
            ("locator", self.locator_check),
            ("governance-bridge", self.governance_check),
        )
        for name, check in hard_gates:
            result = self._safe_check(name, True, check)
            gates.append(result)
            if not result.passed:
                return self._build_report(gates, stages, database, vectord, ollama)
            stages.append(name)

        stages.append("READY")
        return self._build_report(gates, stages, database, vectord, ollama)

    def _build_report(
        self,
        gates: list[GateResult],
        stages: list[str],
        database: dict[str, object] | None,
        vectord: dict[str, object],
        ollama: dict[str, object],
    ) -> StartupReport:
        database = database or {}
        gate_dicts = [gate.to_dict() for gate in gates]
        critical_ok = all(gate.passed for gate in gates if gate.critical)
        degradable_ok = all(gate.passed for gate in gates if not gate.critical)
        if not critical_ok:
            state = STATE_FAILED
        elif degradable_ok:
            state = STATE_READY
        else:
            state = STATE_DEGRADED
        # `ready` reflects whether the shared-layer channel is operational.
        # Critical gates (PostgreSQL + governance) govern channel readiness;
        # degradable RAG/LLM failure lowers state to DEGRADED but does not
        # fail the channel closed.
        ready = critical_ok
        return StartupReport(
            ready,
            tuple(stages),
            database,
            vectord,
            state=state,
            ollama=ollama,
            gates=gate_dicts,
        )


__all__ = ["SharedLayerStartup", "StartupReport", "STATE_READY", "STATE_DEGRADED", "STATE_FAILED", "STATE_RECOVERING"]
