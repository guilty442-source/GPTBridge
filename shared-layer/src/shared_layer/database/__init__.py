"""DEPRECATED PostgreSQL lifecycle for GPTBridge.

Under Governance Codex A44/E30 the SQL engine is local sqlite3
(``shared_layer.local.database``).  This package is declared closed: the
PostgreSQL architecture is not part of the governed runtime and the members
below are exposed lazily so that importing ``shared_layer.database`` does
not require the (uninstalled, prohibited) ``psycopg`` package.
"""

from __future__ import annotations

from typing import Any

_LEGACY_POSTGRESQL_DECLARED_CLOSED: bool = True

_LAZY_EXPORTS = {
    "BootstrapReport": ("bootstrap", "BootstrapReport"),
    "DatabaseBootstrap": ("bootstrap", "DatabaseBootstrap"),
    "BackupOrchestrator": ("backup", "BackupOrchestrator"),
    "BackupResult": ("backup", "BackupResult"),
    "DatabaseSettings": ("config", "DatabaseSettings"),
    "ConnectionManager": ("connection", "ConnectionManager"),
    "PostgreSQLDetection": ("detection", "PostgreSQLDetection"),
    "detect_postgresql": ("detection", "detect_postgresql"),
    "DatabaseHealth": ("health", "DatabaseHealth"),
    "DatabaseHealthCheck": ("health", "DatabaseHealthCheck"),
    "PostgreSQLPool": ("pool", "PostgreSQLPool"),
}

__all__ = ["_LEGACY_POSTGRESQL_DECLARED_CLOSED", *_LAZY_EXPORTS]


def __getattr__(name: str) -> Any:
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module 'shared_layer.database' has no attribute {name!r}")
    module_name, member = entry
    module = __import__(
        f"shared_layer.database.{module_name}", fromlist=[member]
    )
    return getattr(module, member)