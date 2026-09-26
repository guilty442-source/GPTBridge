from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from shared_layer.local import pg_adapter

from .collab_repo_constants import DEFAULT_AGENTS, utc_now
from .collab_repo_schema import CollabRepoSchemaMixin
from .collab_repo_agents import CollabRepoAgentsMixin
from .collab_repo_messages import CollabRepoMessagesMixin
from .collab_repo_memory_tasks import CollabRepoMemoryTasksMixin
from .collab_repo_collab import CollabRepoTasksMixin

__all__ = ["AiCollaborationRepository", "DEFAULT_AGENTS", "utc_now"]

PG_SCHEMA = "gptbridge_collab"


class AiCollaborationRepository(
    CollabRepoSchemaMixin,
    CollabRepoAgentsMixin,
    CollabRepoMessagesMixin,
    CollabRepoMemoryTasksMixin,
    CollabRepoTasksMixin,
):
    def __init__(self, project_root: Path) -> None:
        # A610/A621: PostgreSQL is the sole structured-data authority; the
        # retired ai_collaboration.sqlite3 store was migrated into the
        # ``gptbridge_collab`` schema via the governed sqlite_to_pg
        # pipeline with ledger evidence.
        # AI_COLLAB_PG_SCHEMA lets test fixtures isolate a throwaway schema.
        self._schema = str(os.environ.get("AI_COLLAB_PG_SCHEMA") or PG_SCHEMA)
        self.db_path = f"postgresql:{self._schema}"
        self._ensure_schema()
        self._ensure_default_agents()

    def _connect(self) -> Any:
        return pg_adapter.connect(self._schema)
