from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .privacy import (
    decode_binary_document,
    decode_json_document,
    encode_binary_document,
    privacy_status,
    protect_text,
    unprotect_text,
)
from .watch_repository import (
    _iter_migration_files,
    _validated_storage_path,
)
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


from .analytics_store_schema import InvestmentAnalyticsStoreSchema
from .analytics_store_queries import InvestmentAnalyticsStoreQueries
from .analytics_store_ledger import AnalyticsStoreLedgerMixin
from .analytics_store_metrics import AnalyticsStoreMetricsMixin
from .analytics_store_backtest import AnalyticsStoreBacktestMixin
from .analytics_store_calibration import AnalyticsStoreCalibrationMixin


class InvestmentAnalyticsStore(
    InvestmentAnalyticsStoreSchema,
    InvestmentAnalyticsStoreQueries,
    AnalyticsStoreLedgerMixin,
    AnalyticsStoreMetricsMixin,
    AnalyticsStoreBacktestMixin,
    AnalyticsStoreCalibrationMixin,
):

    def audit(self, action: str, details: dict[str, Any], *, severity: str = "info") -> None:
        audit_id = uuid.uuid4().hex
        occurred_at = utc_text()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(audit_id, occurred_at, action, severity, details_encrypted) VALUES(?, ?, ?, ?, ?)",
                (audit_id, occurred_at, action, severity, protect_text(_json(details))),
            )
        self._append_managed_audit_record(
            audit_id=audit_id,
            occurred_at=occurred_at,
            action=action,
            severity=severity,
            details=details,
        )


    def _append_managed_audit_record(
        self,
        *,
        audit_id: str,
        occurred_at: str,
        action: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        record = {
            "audit_id": audit_id,
            "occurred_at": occurred_at,
            "tool_id": "ai-assistant",
            "action": action,
            "severity": severity,
            "details_encrypted": protect_text(_json(details)),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())


    def list_audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT audit_id, occurred_at, action, severity, details_encrypted FROM audit_log ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["details"] = _decoded_json(
                unprotect_text(str(item.pop("details_encrypted", "") or "")), {}
            )
            output.append(item)
        return output


    def close(self) -> None:
        with self._database_lock:
            if self._closed:
                return
            try:
                self._persist_database()
            finally:
                try:
                    self._database_connection.close()
                finally:
                    self._closed = True
                    self._owner_lock.release()


    def analytics_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        with self.batch_updates():
            return self._analytics_snapshot(state)


    def _analytics_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        performance = self.performance(state)
        ledger = performance.get("ledger", {})
        risk = self.risk(state)
        self.update_decision_outcomes()
        triggered = self.evaluate_alerts(state, risk)
        counts, tombstone_count = self._table_counts()
        return {
            "version": "1.0.0",
            "generated_at": utc_text(),
            "database_path": str(self.database_path),
            "data_health": self._data_health(counts, tombstone_count, state, ledger, risk),
            "privacy": privacy_status(),
            "performance": performance,
            "risk": risk,
            "stress": self.stress_test(state),
            "ledger": {
                "transactions": self.list_transactions(100),
                **ledger,
                "reconciliation": self.reconcile_ledger_holdings(state),
            },
            "events": self.list_events(100),
            "alerts": self._alert_summary(triggered),
            "decisions": self.decisions(100),
            "calibration": self.calibration(),
        }


    def _table_counts(self) -> tuple[dict[str, int], int]:
        with self.connect() as connection:
            counts = {
                table: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
                        if table == "transactions"
                        else f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "transactions",
                    "prices",
                    "portfolio_snapshots",
                    "market_events",
                    "alert_rules",
                    "alert_events",
                    "decisions",
                )
            }
            tombstone_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transactions WHERE deleted_at <> ''"
                ).fetchone()[0]
            )
        return counts, tombstone_count


    def _data_health(
        self,
        counts: dict[str, int],
        tombstone_count: int,
        state: dict[str, Any],
        ledger: dict[str, Any],
        risk: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "counts": counts,
            "transaction_tombstone_count": tombstone_count,
            "history_ready": counts["prices"] >= max(30, len(state.get("holdings", [])) * 30),
            "ledger_ready": counts["transactions"] > 0,
            "ledger_quality": ledger.get("ledger_quality", "empty"),
            "warnings": [
                message
                for condition, message in (
                    (counts["transactions"] == 0, "尚未建立交易帳本，XIRR 與已實現損益可能不完整。"),
                    (risk.get("status") != "ready", "歷史行情不足，專業風險指標暫不完整。"),
                )
                if condition
            ],
        }


    def _alert_summary(self, triggered: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "rules": self.list_alert_rules(),
            "events": self.list_alert_events(100),
            "new_count": len(triggered),
            "unacknowledged_count": sum(1 for item in self.list_alert_events(500) if not item.get("acknowledged_at")),
        }


POSITIVE_WORDS = {"beat", "growth", "raise", "upgrade", "profit", "surge", "record", "成長", "上修", "獲利", "創高", "優於"}
NEGATIVE_WORDS = {"miss", "cut", "downgrade", "loss", "fall", "risk", "fraud", "下修", "虧損", "衰退", "風險", "裁員"}



__all__ = ['InvestmentAnalyticsUpgradeRequired', 'SCHEMA_VERSION', 'DEFAULT_ALERT_COOLDOWN_MINUTES', 'OPENING_BALANCE_PREFIX', 'RECONCILIATION_PREFIX', 'DEFAULT_BACKUP_RETENTION', 'DEFAULT_AUTOMATIC_BACKUP_RETENTION', 'InvestmentAnalyticsStore']
