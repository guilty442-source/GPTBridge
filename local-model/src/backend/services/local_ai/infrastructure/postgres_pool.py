from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_POOL_MIN: int = 1
_POOL_MAX: int = 8


def require_dsn(environment_key: str) -> str:
    value = str(os.environ.get(environment_key) or "").strip()
    if not value:
        raise RuntimeError(f"{environment_key}_REQUIRED")
    return value


def require_tool_dsn(environment_key: str, tool_root: Path) -> str:
    value = str(os.environ.get(environment_key) or "").strip()
    if value:
        return value
    configured = Path(tool_root) / ".env" / "postgres_dsn"
    if configured.is_file():
        value = configured.read_text(encoding="utf-8").strip()
    if value:
        return value
    raise RuntimeError(f"{environment_key}_REQUIRED")


class PostgresPoolManager:
    """Centralized, per-DSN connection pool management for the module's
    PostgreSQL repositories (RAG index, cognition, identity).

    Deduplicates pools by DSN so all repositories share connections instead of
    each opening its own pool. Writes go through psycopg_pool.ConnectionPool just
    like the RAG repository, and reads use the same wrapped connection objects.
    """

    _POOL_MIN: int = _POOL_MIN
    _POOL_MAX: int = _POOL_MAX

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self._pools: dict[str, ConnectionPool] = {}
        self._opened: bool = False
        self._rag_dsn: str = ""
        self._cognition_dsn: str = ""
        self._identity_dsn: str = ""

    def _snapshot_dsns(self) -> None:
        if self._opened:
            return
        rag = str(os.environ.get("GPTBRIDGE_POSTGRES_DSN") or "").strip()
        cognition = str(
            os.environ.get("GPTBRIDGE_XINGCHENG_COGNITION_DSN") or ""
        ).strip()
        identity = str(
            os.environ.get("GPTBRIDGE_XINGCHENG_IDENTITY_DSN") or ""
        ).strip()
        if not rag:
            rag = self._tool_dsn("GPTBRIDGE_POSTGRES_DSN")
        if not cognition:
            cognition = self._tool_dsn("GPTBRIDGE_XINGCHENG_COGNITION_DSN")
        if not identity:
            identity = self._tool_dsn("GPTBRIDGE_XINGCHENG_IDENTITY_DSN")
        self._rag_dsn = rag
        self._cognition_dsn = cognition
        self._identity_dsn = identity
        for key, dsn in (
            ("rag", rag),
            ("cognition", cognition),
            ("identity", identity),
        ):
            if dsn:
                self._pools[key] = self._open_pool(dsn)
        self._opened = True

    def _tool_dsn(self, environment_key: str) -> str:
        configured = self.tool_root / "local-ai" / ".env" / "credentials" / "postgres"
        if configured.is_file():
            value = configured.read_text(encoding="utf-8").strip()
            if value:
                return value
        raise RuntimeError(f"{environment_key}_REQUIRED")

    @staticmethod
    def _open_pool(dsn: str) -> ConnectionPool:
        return ConnectionPool(
            conninfo=dsn,
            min_size=_POOL_MIN,
            max_size=_POOL_MAX,
            kwargs={"row_factory": dict_row},
            open=True,
        )

    def pool(self, key: str) -> ConnectionPool:
        self._snapshot_dsns()
        if key not in self._pools:
            raise RuntimeError(f"POSTGRES_POOL_UNAVAILABLE:{key}")
        return self._pools[key]

    def identity_pool(self) -> ConnectionPool:
        self._snapshot_dsns()
        if "identity" not in self._pools:
            raise RuntimeError("GPTBRIDGE_XINGCHENG_IDENTITY_DSN_REQUIRED")
        return self._pools["identity"]

    def identity_pool_or_none(self) -> ConnectionPool | None:
        self._snapshot_dsns()
        return self._pools.get("identity")

    def identity_pool_available(self) -> bool:
        self._snapshot_dsns()
        return bool(self._pools.get("identity"))

    def cognition_pool(self) -> ConnectionPool:
        self._snapshot_dsns()
        if "cognition" not in self._pools:
            raise RuntimeError("GPTBRIDGE_XINGCHENG_COGNITION_DSN_REQUIRED")
        return self._pools["cognition"]

    def knowledge_pool(self) -> ConnectionPool:
        return self.cognition_pool()

    def close(self) -> None:
        for pool in self._pools.values():
            try:
                pool.close()
            except Exception:
                pass
        self._pools.clear()
        self._opened = False

    def status(self) -> dict[str, Any]:
        self._snapshot_dsns()
        return {
            "engine": "postgresql",
            "pooled": True,
            "pool_min": _POOL_MIN,
            "pool_max": _POOL_MAX,
            "databases": {
                key: {"available": bool(dsn)}
                for key, dsn in (
                    ("rag", self._rag_dsn),
                    ("cognition", self._cognition_dsn),
                    ("identity", self._identity_dsn),
                )
            },
        }

    @property
    def rag_dsn(self) -> str:
        self._snapshot_dsns()
        return self._rag_dsn

    @property
    def cognition_dsn(self) -> str:
        self._snapshot_dsns()
        return self._cognition_dsn

    @property
    def identity_dsn(self) -> str:
        self._snapshot_dsns()
        return self._identity_dsn


@lru_cache(maxsize=1)
def get_pool_manager(tool_root: Path) -> PostgresPoolManager:
    return PostgresPoolManager(tool_root)


__all__ = ["PostgresPoolManager", "get_pool_manager"]
