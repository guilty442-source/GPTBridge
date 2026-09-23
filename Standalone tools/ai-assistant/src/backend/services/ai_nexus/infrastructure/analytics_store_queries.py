from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from ..domain.contract import INVESTMENT_ANALYTICS_SCHEMA_VERSION


SCHEMA_VERSION = INVESTMENT_ANALYTICS_SCHEMA_VERSION
DEFAULT_ALERT_COOLDOWN_MINUTES = 240
OPENING_BALANCE_PREFIX = "opening-balance:"
RECONCILIATION_PREFIX = "ledger-reconciliation:"
DEFAULT_BACKUP_RETENTION = 1
DEFAULT_AUTOMATIC_BACKUP_RETENTION = 1


from .analytics_helpers import *
from .analytics_finance import *
from .analytics_io import *
from .analytics_common import (
    InvestmentAnalyticsUpgradeRequired,
    _portable_sqlite_image,
)

from .analytics_store_imports import AnalyticsStoreImportsMixin
from .analytics_store_transactions import AnalyticsStoreTransactionsMixin
from .analytics_store_market_data import AnalyticsStoreMarketDataMixin
from .analytics_store_alerts import AnalyticsStoreAlertsMixin
from .analytics_store_decisions import AnalyticsStoreDecisionsMixin


class InvestmentAnalyticsStoreQueries(
    AnalyticsStoreImportsMixin,
    AnalyticsStoreTransactionsMixin,
    AnalyticsStoreMarketDataMixin,
    AnalyticsStoreAlertsMixin,
    AnalyticsStoreDecisionsMixin,
):

    def clear_data(self, *, permanent: bool = False) -> dict[str, Any]:
        safety = (
            {"ok": True, "created": False, "permanent": True, "path": ""}
            if permanent
            else self.backup_database("before-clear")
        )
        with self.connect() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            for table in self._table_deletion_order(connection):
                connection.execute(f"DELETE FROM {table}")  # sql-ok: per-table delete in FK-safe order, bounded by table count
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'sqlite_sequence'"
            ).fetchone():
                connection.execute("DELETE FROM sqlite_sequence")
        if permanent:
            self._purge_permanent_data()
        return safety


    def _table_deletion_order(self, connection: sqlite3.Connection) -> list[str]:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT IN ('metadata', 'sqlite_sequence')"
            ).fetchall()
        ]
        table_names = set(tables)
        child_to_parents: dict[str, set[str]] = {table: set() for table in tables}
        parent_indegree: dict[str, int] = {table: 0 for table in tables}
        for table in tables:
            if not table.replace("_", "").isalnum():
                raise RuntimeError("invalid analytics table name")
            parents = {
                str(row[2])
                for row in connection.execute(  # sql-ok: PRAGMA introspection per table
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()
                if str(row[2]) in table_names and str(row[2]) != table
            }
            child_to_parents[table] = parents
            for parent in parents:
                parent_indegree[parent] += 1
        ready = sorted(
            table for table, indegree in parent_indegree.items() if indegree == 0
        )
        deletion_order: list[str] = []
        while ready:
            table = ready.pop(0)
            deletion_order.append(table)
            for parent in sorted(child_to_parents[table]):
                parent_indegree[parent] -= 1
                if parent_indegree[parent] == 0:
                    ready.append(parent)
                    ready.sort()
        deletion_order.extend(
            sorted(table_names.difference(deletion_order))
        )
        return deletion_order


    def _purge_permanent_data(self) -> None:
        roots = [self.backup_root, self.recovery_root]
        if not self._uses_external_managed_storage:
            roots.append(self.audit_root)
        for root in roots:
            if not root.is_dir():
                continue
            for current_root, directory_names, file_names in os.walk(
                root,
                topdown=False,
                followlinks=False,
            ):
                current_path = Path(current_root)
                for name in file_names:
                    (current_path / name).unlink()
                for name in directory_names:
                    child = current_path / name
                    if child.is_symlink():
                        child.unlink()
                    else:
                        child.rmdir()
        with self._database_lock:
            self._database_connection.execute("VACUUM")
            self._persist_database(rotate_key=True)


    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, _json(value), utc_text()),
            )


    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return _decoded_json(row["value_json"], default) if row else default


__all__ = ['InvestmentAnalyticsStoreQueries']
