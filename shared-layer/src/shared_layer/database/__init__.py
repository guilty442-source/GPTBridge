"""PostgreSQL lifecycle for GPTBridge's canonical structured data.

PostgreSQL owns central structured data, shared transport, and audit roles.
The local SQLite package is limited to owner-private state and bounded,
observable degraded operation that must reconcile back to PostgreSQL.
Members remain lazy so health inspection does not require an eager connection.
"""

from __future__ import annotations

from typing import Any

POSTGRESQL_CANONICAL: bool = True

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

__all__ = ["POSTGRESQL_CANONICAL", *_LAZY_EXPORTS]


def __getattr__(name: str) -> Any:
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module 'shared_layer.database' has no attribute {name!r}")
    module_name, member = entry
    module = __import__(
        f"shared_layer.database.{module_name}", fromlist=[member]
    )
    return getattr(module, member)