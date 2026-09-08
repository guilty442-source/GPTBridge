from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

_POOL_MIN: int = 1
_POOL_MAX: int = 8

RAG_DATABASE_NAME = "local-rag-keywords.sqlite3"
COGNITION_DATABASE_NAME = "cognition.sqlite3"
IDENTITY_DATABASE_NAME = "identity.sqlite3"


class LocalSqlitePoolManager:
    """Local sqlite3 database manager for the module repositories.

    Governs the three local databases under ``tool_root`` — the RAG keyword
    store, the Xingcheng cognition store and the Xingcheng identity store.
    No PostgreSQL/psycopg, no external service (A44/E30).
    """

    _POOL_MIN: int = _POOL_MIN
    _POOL_MAX: int = _POOL_MAX

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self._paths: dict[str, Path] = {
            "rag": self.tool_root / "runtime" / "state" / RAG_DATABASE_NAME,
            "cognition": (
                self.tool_root / "xingcheng" / "runtime" / "state" / COGNITION_DATABASE_NAME
            ),
            "identity": (
                self.tool_root / "xingcheng" / "runtime" / "state" / IDENTITY_DATABASE_NAME
            ),
        }

    def _database_path(self, key: str) -> Path:
        path = self._paths[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def pool(self, key: str) -> "LocalSqlitePoolManager":
        if key not in self._paths:
            raise RuntimeError(f"SQLITE_POOL_UNAVAILABLE:{key}")
        return self

    def _connection(self, key: str) -> sqlite3.Connection:
        path = self._database_path(key)
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self, key: str) -> Iterator[sqlite3.Connection]:
        connection = self._connection(key)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def identity_pool(self) -> "LocalSqlitePoolManager":
        if "identity" not in self._paths:
            raise RuntimeError("GPTBRIDGE_XINGCHENG_IDENTITY_DB_UNAVAILABLE")
        return self

    def identity_pool_or_none(self) -> "LocalSqlitePoolManager | None":
        return self

    def identity_pool_available(self) -> bool:
        return self._paths["identity"].parent.exists() or True

    def cognition_pool(self) -> "LocalSqlitePoolManager":
        if "cognition" not in self._paths:
            raise RuntimeError("GPTBRIDGE_XINGCHENG_COGNITION_DB_UNAVAILABLE")
        return self

    def knowledge_pool(self) -> "LocalSqlitePoolManager":
        return self.cognition_pool()

    def close(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "engine": "local-sqlite3-degraded",
            "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "non-canonical-reconciliation-required",
            "reconciliation_required": True,
            "pooled": True,
            "pool_min": _POOL_MIN,
            "pool_max": _POOL_MAX,
            "databases": {
                key: {"available": True, "path": str(path)}
                for key, path in self._paths.items()
            },
        }

    @property
    def rag_dsn(self) -> str:
        return str(self._paths["rag"])

    @property
    def cognition_dsn(self) -> str:
        return str(self._paths["cognition"])

    @property
    def identity_dsn(self) -> str:
        return str(self._paths["identity"])


@lru_cache(maxsize=1)
def get_pool_manager(tool_root: Path) -> LocalSqlitePoolManager:
    return LocalSqlitePoolManager(tool_root)


def require_dsn(environment_key: str) -> str:
    value = str(os.environ.get(environment_key) or "").strip()
    if not value:
        raise RuntimeError(f"{environment_key}_REQUIRED")
    return value


def require_tool_dsn(environment_key: str, tool_root: Path) -> str:
    return require_dsn(environment_key)


__all__ = [
    "LocalSqlitePoolManager",
    "get_pool_manager",
    "require_dsn",
    "require_tool_dsn",
]