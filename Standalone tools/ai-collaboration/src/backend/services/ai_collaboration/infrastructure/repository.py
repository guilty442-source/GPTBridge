from __future__ import annotations

import sqlite3
from pathlib import Path

from .collab_repo_constants import DEFAULT_AGENTS, utc_now
from .collab_repo_schema import CollabRepoSchemaMixin
from .collab_repo_agents import CollabRepoAgentsMixin
from .collab_repo_messages import CollabRepoMessagesMixin
from .collab_repo_memory_tasks import CollabRepoMemoryTasksMixin

__all__ = ["AiCollaborationRepository", "DEFAULT_AGENTS", "utc_now"]


class AiCollaborationRepository(
    CollabRepoSchemaMixin,
    CollabRepoAgentsMixin,
    CollabRepoMessagesMixin,
    CollabRepoMemoryTasksMixin,
):
    def __init__(self, project_root: Path) -> None:
        self.db_path = project_root / "runtime" / "state" / "ai_collaboration.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._ensure_default_agents()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection
