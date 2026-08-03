from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .database.bootstrap import DatabaseBootstrap
from .database.config import DatabaseSettings
from .database.health import DatabaseHealthCheck


@dataclass(frozen=True)
class StartupReport:
    ready: bool
    stages: tuple[str, ...]
    database: dict[str, object]
    qdrant: dict[str, object]


class SharedLayerStartup:
    """Fail-closed startup sequence: READY exists only after every required gate."""

    def __init__(
        self,
        shared_root: Path | str,
        settings: DatabaseSettings,
        qdrant_health: Callable[[], dict[str, object]],
        registry_check: Callable[[], bool],
        locator_check: Callable[[], bool],
        governance_check: Callable[[], bool],
    ) -> None:
        self.root = Path(shared_root).resolve()
        self.settings = settings
        self.qdrant_health = qdrant_health
        self.registry_check = registry_check
        self.locator_check = locator_check
        self.governance_check = governance_check

    def run(self) -> StartupReport:
        stages: list[str] = []
        DatabaseBootstrap(
            self.settings,
            self.root / "migrations",
            self.root / "sql" / "central_index.sql",
        ).run()
        stages.extend(("database-bootstrap", "role-schema-migration-rls-index"))
        database = asdict(DatabaseHealthCheck(self.settings).run())
        if not database["available"]:
            return StartupReport(False, tuple(stages), database, {})
        stages.append("database-health")
        qdrant = self.qdrant_health()
        if qdrant.get("available") is not True:
            return StartupReport(False, tuple(stages), database, qdrant)
        stages.append("qdrant-health-collection")
        for name, check in (
            ("registry", self.registry_check),
            ("locator", self.locator_check),
            ("governance-bridge", self.governance_check),
        ):
            if check() is not True:
                return StartupReport(False, tuple(stages), database, qdrant)
            stages.append(name)
        stages.append("READY")
        return StartupReport(True, tuple(stages), database, qdrant)


__all__ = ["SharedLayerStartup", "StartupReport"]
