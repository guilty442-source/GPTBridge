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


class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""


def _portable_sqlite_image(data: bytes) -> bytes:
    """Make serialized WAL databases self-contained for memory deserialization."""
    if len(data) >= 20 and data.startswith(b"SQLite format 3\x00") and data[18:20] == b"\x02\x02":
        normalized = bytearray(data)
        normalized[18] = 1
        normalized[19] = 1
        return bytes(normalized)
    return data



from .analytics_helpers import *
from .analytics_finance import *
from .analytics_io import *
class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""




class InvestmentAnalyticsStoreQueries:

    def clear_data(self, *, permanent: bool = False) -> dict[str, Any]:
        safety = (
            {"ok": True, "created": False, "permanent": True, "path": ""}
            if permanent
            else self.backup_database("before-clear")
        )
        with self.connect() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
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
                    for row in connection.execute(
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
            for table in deletion_order:
                connection.execute(f"DELETE FROM {table}")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'sqlite_sequence'"
            ).fetchone():
                connection.execute("DELETE FROM sqlite_sequence")
        if permanent:
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
        return safety


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


    @staticmethod
    def _public_import_operation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = _decoded_json(
            unprotect_text(str(item.pop("payload_encrypted", "") or "")),
            {},
        )
        item["result"] = _decoded_json(
            unprotect_text(str(item.pop("result_encrypted", "") or "")),
            {},
        )
        item["error"] = unprotect_text(
            str(item.pop("error_encrypted", "") or "")
        )
        item["history"] = _decoded_json(
            unprotect_text(str(item.pop("history_encrypted", "") or "")),
            [],
        )
        return item


    def get_import_operation(self, operation_id: str) -> dict[str, Any] | None:
        normalized = str(operation_id or "").strip()
        if not normalized:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
        return self._public_import_operation(row) if row is not None else None


    def create_or_resume_import_operation(
        self,
        request_fingerprint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint = str(request_fingerprint or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("invalid import request fingerprint")
        now = utc_text()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                operation_id = uuid.uuid4().hex
                history = [
                    {
                        "status": "queued",
                        "occurred_at": now,
                        "reason": "request_created",
                    }
                ]
                connection.execute(
                    """
                    INSERT INTO import_operations(
                        operation_id, request_fingerprint, import_fingerprint,
                        status, payload_encrypted, result_encrypted,
                        error_encrypted, history_encrypted, attempt_count,
                        created_at, updated_at, started_at, finished_at
                    ) VALUES(?, ?, '', 'queued', ?, '', '', ?, 0, ?, ?, '', '')
                    """,
                    (
                        operation_id,
                        fingerprint,
                        protect_text(_json(payload)),
                        protect_text(_json(history)),
                        now,
                        now,
                    ),
                )
            elif str(row["status"] or "") == "failed":
                history = _decoded_json(
                    unprotect_text(str(row["history_encrypted"] or "")),
                    [],
                )
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        "status": "queued",
                        "occurred_at": now,
                        "reason": "explicit_retry",
                    }
                )
                connection.execute(
                    """
                    UPDATE import_operations
                    SET status='queued', payload_encrypted=?, result_encrypted='',
                        error_encrypted='', history_encrypted=?, updated_at=?,
                        finished_at=''
                    WHERE operation_id=?
                    """,
                    (
                        protect_text(_json(payload)),
                        protect_text(_json(history)),
                        now,
                        str(row["operation_id"]),
                    ),
                )
            operation_row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if operation_row is None:
            raise RuntimeError("unable to persist import operation")
        return self._public_import_operation(operation_row)


    def update_import_operation(
        self,
        operation_id: str,
        *,
        status: str,
        reason: str = "",
        import_fingerprint: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        normalized = str(operation_id or "").strip()
        normalized_status = str(status or "").strip().casefold()
        allowed_statuses = {
            "queued",
            "processing",
            "resume_pending",
            "completed",
            "failed",
        }
        if not normalized or normalized_status not in allowed_statuses:
            raise ValueError("invalid import operation update")
        now = utc_text()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise ValueError("import operation not found")
            current_status = str(row["status"] or "")
            if current_status == "completed" and normalized_status != "completed":
                return self._public_import_operation(row)
            history = _decoded_json(
                unprotect_text(str(row["history_encrypted"] or "")),
                [],
            )
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "status": normalized_status,
                    "occurred_at": now,
                    "reason": str(reason or ""),
                }
            )
            next_import_fingerprint = (
                str(import_fingerprint or "").strip().lower()
                if import_fingerprint is not None
                else str(row["import_fingerprint"] or "")
            )
            next_result = (
                protect_text(_json(result))
                if result is not None
                else str(row["result_encrypted"] or "")
            )
            next_error = (
                protect_text(str(error or ""))
                if error is not None
                else str(row["error_encrypted"] or "")
            )
            started_at = (
                now
                if normalized_status == "processing"
                and not str(row["started_at"] or "")
                else str(row["started_at"] or "")
            )
            finished_at = (
                now
                if normalized_status in {"completed", "failed"}
                else ""
                if normalized_status in {"queued", "resume_pending"}
                else str(row["finished_at"] or "")
            )
            connection.execute(
                """
                UPDATE import_operations
                SET import_fingerprint=?, status=?, result_encrypted=?,
                    error_encrypted=?, history_encrypted=?,
                    attempt_count=attempt_count+?, updated_at=?,
                    started_at=?, finished_at=?
                WHERE operation_id=?
                """,
                (
                    next_import_fingerprint,
                    normalized_status,
                    next_result,
                    next_error,
                    protect_text(_json(history)),
                    1 if increment_attempt else 0,
                    now,
                    started_at,
                    finished_at,
                    normalized,
                ),
            )
            updated = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("import operation disappeared after update")
        return self._public_import_operation(updated)


    def resumable_import_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations
                WHERE status IN ('queued', 'processing', 'resume_pending')
                ORDER BY created_at ASC LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [self._public_import_operation(row) for row in rows]


    def add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        side = str(payload.get("side") or "BUY").strip().upper()
        if side not in {"BUY", "SELL", "DIVIDEND", "FEE", "CASH_IN", "CASH_OUT"}:
            raise ValueError("unsupported transaction side")
        symbol = str(payload.get("symbol") or "").strip().upper()
        if side in {"BUY", "SELL", "DIVIDEND"} and not symbol:
            raise ValueError("transaction symbol is required")
        quantity = max(0.0, number(payload.get("quantity")))
        price = max(0.0, number(payload.get("price")))
        amount = max(0.0, number(payload.get("amount")))
        if side in {"DIVIDEND", "FEE", "CASH_IN", "CASH_OUT"} and price <= 0:
            price = amount
        if side in {"BUY", "SELL"} and (quantity <= 0 or price <= 0):
            raise ValueError("buy/sell transaction requires positive quantity and price")
        occurred = parse_datetime(payload.get("occurred_at")) or utc_now()
        transaction_id = str(payload.get("transaction_id") or uuid.uuid4().hex)
        row = {
            "transaction_id": transaction_id,
            "occurred_at": utc_text(occurred),
            "symbol": symbol,
            "market": str(payload.get("market") or "").strip().upper(),
            "asset_type": str(payload.get("asset_type") or "").strip().upper(),
            "side": side,
            "quantity": quantity,
            "price": price,
            "currency": str(payload.get("currency") or "").strip().upper(),
            "fee": max(0.0, number(payload.get("fee"))),
            "tax": max(0.0, number(payload.get("tax"))),
            "note_encrypted": protect_text(str(payload.get("note") or "")),
            "created_at": utc_text(),
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_id, occurred_at, symbol, market, asset_type, side,
                    quantity, price, currency, fee, tax, note_encrypted, created_at
                ) VALUES(
                    :transaction_id, :occurred_at, :symbol, :market, :asset_type, :side,
                    :quantity, :price, :currency, :fee, :tax, :note_encrypted, :created_at
                )
                ON CONFLICT(transaction_id) DO UPDATE SET
                    occurred_at=excluded.occurred_at, symbol=excluded.symbol,
                    market=excluded.market, asset_type=excluded.asset_type,
                    side=excluded.side, quantity=excluded.quantity,
                    price=excluded.price, currency=excluded.currency,
                    fee=excluded.fee, tax=excluded.tax,
                    note_encrypted=excluded.note_encrypted,
                    deleted_at='', delete_reason_encrypted='',
                    delete_audit_id=''
                """,
                row,
            )
        public = dict(row)
        encrypted_note = str(public.pop("note_encrypted", ""))
        public["note"] = unprotect_text(encrypted_note)
        return public


    def delete_transaction(
        self,
        transaction_id: str,
        *,
        reason: str = "user_requested",
    ) -> bool:
        normalized = str(transaction_id or "").strip()
        if not normalized:
            return False
        deleted_at = utc_text()
        audit_id = uuid.uuid4().hex
        audit_details: dict[str, Any] = {}
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT transaction_id, occurred_at, symbol, market, asset_type, side, quantity, price, currency, fee, tax, note_encrypted, created_at, deleted_at, delete_reason_encrypted, delete_audit_id FROM transactions
                WHERE transaction_id = ? AND deleted_at = ''
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """
                UPDATE transactions
                SET deleted_at=?, delete_reason_encrypted=?,
                    delete_audit_id=?
                WHERE transaction_id=? AND deleted_at=''
                """,
                (
                    deleted_at,
                    protect_text(str(reason or "user_requested")),
                    audit_id,
                    normalized,
                ),
            )
            audit_details = {
                "transaction_id": normalized,
                "reason": str(reason or "user_requested"),
                "preserved_row": dict(row),
                "physical_delete": False,
            }
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, occurred_at, action, severity, details_encrypted
                ) VALUES(?, ?, 'transaction_tombstoned', 'warning', ?)
                """,
                (
                    audit_id,
                    deleted_at,
                    protect_text(
                        _json(
                            audit_details
                        )
                    ),
                ),
            )
        changed = bool(cursor.rowcount)
        if changed:
            self._append_managed_audit_record(
                audit_id=audit_id,
                occurred_at=deleted_at,
                action="transaction_tombstoned",
                severity="warning",
                details=audit_details,
            )
        return changed


    def list_transactions(
        self,
        limit: int = 500,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                (
                    "SELECT transaction_id, occurred_at, symbol, market, asset_type, side, quantity, price, currency, fee, tax, note_encrypted, created_at, deleted_at, delete_reason_encrypted, delete_audit_id FROM transactions "
                    + ("" if include_deleted else "WHERE deleted_at = '' ")
                    + "ORDER BY occurred_at DESC, created_at DESC LIMIT ?"
                ),
                (max(1, min(5000, int(limit))),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["note"] = unprotect_text(str(item.pop("note_encrypted", "") or ""))
            item["delete_reason"] = unprotect_text(
                str(item.pop("delete_reason_encrypted", "") or "")
            )
            item["deleted"] = bool(item.get("deleted_at"))
            item["is_estimated"] = str(item.get("transaction_id") or "").startswith(
                (OPENING_BALANCE_PREFIX, RECONCILIATION_PREFIX)
            )
            item["source"] = (
                "opening_balance_estimate"
                if str(item.get("transaction_id") or "").startswith(OPENING_BALANCE_PREFIX)
                else "ledger_reconciliation_estimate"
                if str(item.get("transaction_id") or "").startswith(RECONCILIATION_PREFIX)
                else "confirmed_or_manual"
            )
            result.append(item)
        return result


    def sync_opening_balance_transactions(
        self,
        state: dict[str, Any],
        occurred_at: Any,
    ) -> dict[str, Any]:
        opening_at = parse_datetime(occurred_at)
        if opening_at is None:
            raise ValueError("opening balance date is required")
        if opening_at > utc_now():
            raise ValueError("opening balance date cannot be in the future")

        holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        active_count = 0
        uncovered_symbols: list[str] = []
        rows: list[dict[str, Any]] = []
        for index, holding in enumerate(holdings):
            quantity = number(holding.get("quantity"))
            if quantity <= 0:
                continue
            active_count += 1
            symbol = str(holding.get("symbol") or "").strip().upper()
            principal_twd = number(holding.get("principal_twd"))
            holding_currency = str(holding.get("currency") or "TWD").strip().upper()
            if principal_twd <= 0 and holding_currency == "TWD":
                principal_twd = number(holding.get("principal_amount"))
                if principal_twd <= 0:
                    principal_twd = number(holding.get("average_cost")) * quantity
            if not symbol or principal_twd <= 0:
                uncovered_symbols.append(symbol or f"ROW-{index + 1}")
                continue
            identity = str(
                holding.get("holding_id")
                or f"{holding.get('market')}|{symbol}|{index}"
            )
            transaction_id = OPENING_BALANCE_PREFIX + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:24]
            rows.append(
                {
                    "transaction_id": transaction_id,
                    "occurred_at": utc_text(opening_at),
                    "symbol": symbol,
                    "market": str(holding.get("market") or "").strip().upper(),
                    "asset_type": str(holding.get("asset_type") or "").strip().upper(),
                    "side": "BUY",
                    "quantity": quantity,
                    "price": principal_twd / quantity,
                    "currency": "TWD",
                    "fee": 0.0,
                    "tax": 0.0,
                    "note_encrypted": protect_text(
                        "估算期初持股；依目前持股與新台幣本金建立，並非券商成交紀錄。"
                    ),
                    "created_at": utc_text(),
                }
            )

        desired_ids = {row["transaction_id"] for row in rows}
        metadata = {
            "occurred_at": utc_text(opening_at),
            "generated_at": utc_text(),
            "active_holding_count": active_count,
            "generated_count": len(rows),
            "uncovered_count": len(uncovered_symbols),
            "uncovered_symbols": uncovered_symbols[:50],
            "coverage_percent": rounded(len(rows) / active_count * 100, 2)
            if active_count
            else 0.0,
            "methodology": "current_holdings_and_principal_twd",
        }
        with self.connect() as connection:
            confirmed_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM transactions
                    WHERE transaction_id NOT LIKE ? AND deleted_at = ''
                    """,
                    (f"{OPENING_BALANCE_PREFIX}%",),
                ).fetchone()[0]
            )
            if confirmed_count > 0:
                raise ValueError(
                    "confirmed transactions already exist; import broker history instead of rebuilding the opening balance"
                )
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT transaction_id FROM transactions
                    WHERE transaction_id LIKE ? AND deleted_at = ''
                    """,
                    (f"{OPENING_BALANCE_PREFIX}%",),
                ).fetchall()
            }
            stale_ids = existing_ids - desired_ids
            stale_audit_record: tuple[str, str, dict[str, Any]] | None = None
            if stale_ids:
                stale_audit_id = uuid.uuid4().hex
                stale_at = utc_text()
                connection.executemany(
                    """
                    UPDATE transactions
                    SET deleted_at=?,
                        delete_reason_encrypted=?,
                        delete_audit_id=?
                    WHERE transaction_id=? AND deleted_at=''
                    """,
                    [
                        (
                            stale_at,
                            protect_text("opening_balance_replaced"),
                            stale_audit_id,
                            transaction_id,
                        )
                        for transaction_id in sorted(stale_ids)
                    ],
                )
                stale_details = {
                    "transaction_ids": sorted(stale_ids),
                    "reason": "opening_balance_replaced",
                    "physical_delete": False,
                }
                stale_audit_record = (stale_audit_id, stale_at, stale_details)
                connection.execute(
                    """
                    INSERT INTO audit_log(
                        audit_id, occurred_at, action, severity,
                        details_encrypted
                    ) VALUES(
                        ?, ?, 'opening_transactions_tombstoned',
                        'warning', ?
                    )
                    """,
                    (
                        stale_audit_id,
                        stale_at,
                        protect_text(
                            _json(
                                stale_details
                            )
                        ),
                    ),
                )
            if rows:
                connection.executemany(
                    """
                    INSERT INTO transactions(
                        transaction_id, occurred_at, symbol, market, asset_type, side,
                        quantity, price, currency, fee, tax, note_encrypted, created_at
                    ) VALUES(
                        :transaction_id, :occurred_at, :symbol, :market, :asset_type, :side,
                        :quantity, :price, :currency, :fee, :tax, :note_encrypted, :created_at
                    )
                    ON CONFLICT(transaction_id) DO UPDATE SET
                        occurred_at=excluded.occurred_at, symbol=excluded.symbol,
                        market=excluded.market, asset_type=excluded.asset_type,
                        side=excluded.side, quantity=excluded.quantity,
                        price=excluded.price, currency=excluded.currency,
                        fee=excluded.fee, tax=excluded.tax,
                        note_encrypted=excluded.note_encrypted,
                        deleted_at='', delete_reason_encrypted='',
                        delete_audit_id=''
                    """,
                    rows,
                )
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                ("opening_ledger", _json(metadata), utc_text()),
            )
        if stale_audit_record is not None:
            stale_audit_id, stale_at, stale_details = stale_audit_record
            self._append_managed_audit_record(
                audit_id=stale_audit_id,
                occurred_at=stale_at,
                action="opening_transactions_tombstoned",
                severity="warning",
                details=stale_details,
            )
        self.audit("opening_ledger_synced", metadata, severity="warning")
        return metadata


    def add_price_bars(self, bars: Iterable[dict[str, Any]]) -> int:
        prepared: list[tuple[Any, ...]] = []
        for bar in bars:
            symbol = str(bar.get("symbol") or "").strip().upper()
            observed = parse_datetime(bar.get("observed_at") or bar.get("timestamp"))
            close = number(bar.get("close"), -1)
            if not symbol or observed is None or close <= 0:
                continue
            prepared.append(
                (
                    symbol,
                    utc_text(observed),
                    number(bar.get("open"), close),
                    number(bar.get("high"), close),
                    number(bar.get("low"), close),
                    close,
                    number(bar.get("volume")),
                    str(bar.get("currency") or "").upper(),
                    str(bar.get("provider") or "manual"),
                    int(bool(bar.get("verified", True))),
                )
            )
        if not prepared:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO prices(symbol, observed_at, open, high, low, close, volume, currency, provider, verified)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, observed_at, provider) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, currency=excluded.currency,
                    verified=excluded.verified
                """,
                prepared,
            )
        return len(prepared)


    def price_series(self, symbol: str, limit: int = 800) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT observed_at, open, high, low, close, volume, currency, provider, verified
                FROM prices WHERE symbol = ? ORDER BY observed_at DESC LIMIT ?
                """,
                (symbol.strip().upper(), max(2, min(5000, limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]


    def latest_prices(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.* FROM prices p
                INNER JOIN (
                    SELECT symbol, MAX(observed_at) AS observed_at FROM prices GROUP BY symbol
                ) latest ON latest.symbol=p.symbol AND latest.observed_at=p.observed_at
                ORDER BY p.verified DESC, p.provider
                """
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            output.setdefault(str(row["symbol"]), dict(row))
        return output


    def add_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        scheduled = parse_datetime(payload.get("scheduled_at") or payload.get("published_at")) or utc_now()
        symbol = str(payload.get("symbol") or "").strip().upper()
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("event title is required")
        event_type = str(payload.get("event_type") or "news").strip().lower()
        source = str(payload.get("source") or "manual").strip()
        dedupe_source = str(payload.get("dedupe_key") or f"{event_type}|{symbol}|{title}|{utc_text(scheduled)[:16]}")
        dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        row = {
            "event_id": event_id,
            "dedupe_key": dedupe_key,
            "event_type": event_type,
            "symbol": symbol,
            "title": title,
            "scheduled_at": utc_text(scheduled),
            "source": source,
            "source_url_encrypted": protect_text(str(payload.get("source_url") or payload.get("url") or "")),
            "sentiment": rounded(number(payload.get("sentiment")), 4) if payload.get("sentiment") is not None else None,
            "confidence": max(0.0, min(1.0, number(payload.get("confidence"), 0.5))),
            "status": str(payload.get("status") or "scheduled"),
            "details_encrypted": protect_text(_json(payload.get("details") or {})),
            "created_at": utc_text(),
        }
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT event_id, dedupe_key, event_type, symbol, title, scheduled_at, source, source_url_encrypted, sentiment, confidence, status, details_encrypted, created_at FROM market_events WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                existing_details = _decoded_json(
                    unprotect_text(str(existing["details_encrypted"] or "")),
                    {},
                )
                if (
                    existing["sentiment"] == row["sentiment"]
                    and existing["confidence"] == row["confidence"]
                    and existing["status"] == row["status"]
                    and existing_details == (payload.get("details") or {})
                ):
                    return self._public_event(existing)
            connection.execute(
                """
                INSERT INTO market_events VALUES(
                    :event_id, :dedupe_key, :event_type, :symbol, :title,
                    :scheduled_at, :source, :source_url_encrypted, :sentiment,
                    :confidence, :status, :details_encrypted, :created_at
                ) ON CONFLICT(dedupe_key) DO UPDATE SET
                    sentiment=excluded.sentiment, confidence=excluded.confidence,
                    status=excluded.status, details_encrypted=excluded.details_encrypted
                """,
                row,
            )
        return self._public_event(row)


    def add_events(
        self,
        payloads: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist a group of events with one encrypted database write."""

        normalized = [dict(payload) for payload in payloads]
        if not normalized:
            return []
        with self.batch_updates():
            return [self.add_event(payload) for payload in normalized]


    @staticmethod
    def _public_event(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_url"] = unprotect_text(str(item.pop("source_url_encrypted", "") or ""))
        details = unprotect_text(str(item.pop("details_encrypted", "") or ""))
        item["details"] = _decoded_json(details, {})
        return item


    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id, dedupe_key, event_type, symbol, title, scheduled_at, source, source_url_encrypted, sentiment, confidence, status, details_encrypted, created_at FROM market_events ORDER BY scheduled_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [self._public_event(row) for row in rows]


    def ensure_default_alerts(self) -> None:
        with self.connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0])
        if count:
            return
        for rule in (
            {"name": "單一部位超過 35%", "rule_type": "concentration", "operator": ">=", "threshold": 35, "severity": "warning"},
            {"name": "單日 VaR 超過 5%", "rule_type": "var", "operator": ">=", "threshold": 5, "severity": "critical"},
            {"name": "七日內重大事件", "rule_type": "event_window", "operator": "<=", "threshold": 7, "severity": "info"},
        ):
            self.add_alert_rule(rule)


    def add_alert_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule_id = str(payload.get("rule_id") or uuid.uuid4().hex)
        now = utc_text()
        row = {
            "rule_id": rule_id,
            "name": str(payload.get("name") or payload.get("rule_type") or "告警規則"),
            "rule_type": str(payload.get("rule_type") or "price_below"),
            "symbol": str(payload.get("symbol") or "").upper(),
            "operator": str(payload.get("operator") or ">="),
            "threshold": number(payload.get("threshold")) if payload.get("threshold") is not None else None,
            "severity": str(payload.get("severity") or "warning"),
            "cooldown_minutes": max(1, int(number(payload.get("cooldown_minutes"), DEFAULT_ALERT_COOLDOWN_MINUTES))),
            "enabled": int(bool(payload.get("enabled", True))),
            "config_json": _json(payload.get("config") or {}),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_rules VALUES(
                    :rule_id, :name, :rule_type, :symbol, :operator, :threshold,
                    :severity, :cooldown_minutes, :enabled, :config_json,
                    :created_at, :updated_at
                ) ON CONFLICT(rule_id) DO UPDATE SET
                    name=excluded.name, rule_type=excluded.rule_type, symbol=excluded.symbol,
                    operator=excluded.operator, threshold=excluded.threshold,
                    severity=excluded.severity, cooldown_minutes=excluded.cooldown_minutes,
                    enabled=excluded.enabled, config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                row,
            )
        return {**row, "enabled": bool(row["enabled"]), "config": _decoded_json(row["config_json"], {})}


    def list_alert_rules(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT rule_id, name, enabled, config_json, created_at, updated_at FROM alert_rules ORDER BY created_at LIMIT 500"
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"]), "config": _decoded_json(row["config_json"], {})} for row in rows]


    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        return {
            ">": value > threshold,
            ">=": value >= threshold,
            "<": value < threshold,
            "<=": value <= threshold,
            "==": abs(value - threshold) < 1e-9,
        }.get(operator, False)


    def evaluate_alerts(self, state: dict[str, Any], risk: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        positions = self.current_positions(state)
        position_map = {str(item.get("symbol") or ""): item for item in positions}
        risk_data = risk or self.risk(state)
        now = utc_now()
        triggered: list[dict[str, Any]] = []
        events = self.list_events(500)
        for rule in self.list_alert_rules():
            if not rule.get("enabled"):
                continue
            rule_type = str(rule.get("rule_type") or "")
            threshold = number(rule.get("threshold"))
            value: float | None = None
            detail = ""
            if rule_type in {"price_below", "price_above"}:
                position = position_map.get(str(rule.get("symbol") or ""))
                value = number(position.get("price")) if position else None
            elif rule_type == "concentration":
                candidates = [number(item.get("weight_percent")) for item in positions]
                value = max(candidates) if candidates else None
            elif rule_type == "var":
                value = risk_data.get("var_95_one_day_percent")
            elif rule_type == "drawdown":
                value = abs(number(risk_data.get("max_drawdown_percent")))
            elif rule_type == "event_window":
                upcoming = []
                for event in events:
                    scheduled = parse_datetime(event.get("scheduled_at"))
                    if scheduled is None:
                        continue
                    days = (scheduled - now).total_seconds() / 86400
                    if 0 <= days <= threshold and (not rule.get("symbol") or rule.get("symbol") == event.get("symbol")):
                        upcoming.append((days, event))
                if upcoming:
                    upcoming.sort(key=lambda item: item[0])
                    value = upcoming[0][0]
                    detail = str(upcoming[0][1].get("title") or "")
            if value is None:
                continue
            operator = str(rule.get("operator") or ">=")
            condition = value <= threshold if rule_type == "event_window" else self._compare(float(value), operator, threshold)
            if not condition:
                continue
            bucket_minutes = max(1, int(rule.get("cooldown_minutes") or DEFAULT_ALERT_COOLDOWN_MINUTES))
            with self.connect() as connection:
                previous = connection.execute(
                    "SELECT triggered_at FROM alert_events WHERE rule_id = ? ORDER BY triggered_at DESC LIMIT 1",
                    (rule["rule_id"],),
                ).fetchone()
            previous_at = parse_datetime(previous["triggered_at"]) if previous else None
            if previous_at is not None and (now - previous_at).total_seconds() < bucket_minutes * 60:
                continue
            bucket = int(now.timestamp() // (bucket_minutes * 60))
            dedupe_key = f"{rule['rule_id']}:{bucket}"
            event = {
                "alert_event_id": uuid.uuid4().hex,
                "rule_id": rule["rule_id"],
                "dedupe_key": dedupe_key,
                "triggered_at": utc_text(now),
                "severity": rule.get("severity") or "warning",
                "title": rule.get("name") or rule_type,
                "detail": detail or f"目前值 {rounded(float(value), 4)}，條件 {operator} {threshold}",
                "value": float(value),
                "acknowledged_at": "",
            }
            with self.connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO alert_events VALUES(:alert_event_id, :rule_id, :dedupe_key, :triggered_at, :severity, :title, :detail, :value, :acknowledged_at)",
                    event,
                )
            if cursor.rowcount:
                triggered.append(event)
        return triggered


    def list_alert_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT alert_event_id, rule_id, dedupe_key, triggered_at, severity, title, detail, value, acknowledged_at FROM alert_events ORDER BY triggered_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [dict(row) for row in rows]


    def acknowledge_alert(self, alert_event_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE alert_events SET acknowledged_at = ? WHERE alert_event_id = ?",
                (utc_text(), alert_event_id),
            )
        return bool(cursor.rowcount)


    @staticmethod
    def _decision_prediction(
        action: dict[str, Any],
        assessment: dict[str, Any],
    ) -> dict[str, Any]:
        explicit = (
            action.get("prediction")
            if isinstance(action.get("prediction"), dict)
            else assessment.get("prediction")
            if isinstance(assessment.get("prediction"), dict)
            else {}
        )
        aliases = {
            "up": "bullish",
            "positive": "bullish",
            "long": "bullish",
            "buy": "bullish",
            "down": "bearish",
            "negative": "bearish",
            "short": "bearish",
            "sell": "bearish",
            "flat": "neutral",
            "sideways": "neutral",
            "hold": "neutral",
            "monitor": "neutral",
        }
        supplied_direction = str(
            explicit.get("direction")
            or action.get("prediction_direction")
            or ""
        ).strip().lower()
        direction = aliases.get(supplied_direction, supplied_direction)
        explicitly_directional = direction in {"bullish", "bearish", "neutral"}
        if not explicitly_directional:
            action_text = " ".join(
                str(action.get(key) or "") for key in ("action", "title", "detail")
            ).lower()
            if any(
                token in action_text
                for token in (
                    "buy", "add", "increase", "accumulate",
                    "買進", "加碼", "增持", "看漲",
                )
            ):
                direction = "bullish"
            elif any(
                token in action_text
                for token in (
                    "sell", "reduce", "decrease", "exit",
                    "賣出", "減碼", "清倉", "看跌",
                )
            ):
                direction = "bearish"
            elif any(
                token in action_text
                for token in (
                    "hold", "monitor", "observe", "wait",
                    "持有", "觀察", "監控", "等待",
                )
            ):
                direction = "neutral"
            else:
                direction = "abstain"
        horizon_days = max(
            1,
            min(
                3650,
                int(
                    number(
                        explicit.get("horizon_days")
                        if explicit.get("horizon_days") is not None
                        else action.get("horizon_days"),
                        30,
                    )
                ),
            ),
        )
        threshold_default = 2.0 if direction == "neutral" else 0.0
        threshold = max(
            0.0,
            min(
                100.0,
                number(
                    explicit.get("return_threshold_percent")
                    if explicit.get("return_threshold_percent") is not None
                    else explicit.get("threshold_percent")
                    if explicit.get("threshold_percent") is not None
                    else action.get("return_threshold_percent"),
                    threshold_default,
                ),
            ),
        )
        if "eligible_for_calibration" in explicit:
            eligible = bool(explicit.get("eligible_for_calibration"))
        else:
            # Inferred hold/monitor text is guidance, not a directional forecast.
            eligible = direction in {"bullish", "bearish"} or (
                direction == "neutral" and explicitly_directional
            )
        return {
            "direction": direction,
            "horizon_days": horizon_days,
            "return_threshold_percent": threshold,
            "eligible_for_calibration": bool(
                eligible and direction in {"bullish", "bearish", "neutral"}
            ),
            "source": "explicit" if explicitly_directional else "action_text_inference",
        }


    def record_decisions(self, analysis: dict[str, Any]) -> int:
        command = analysis.get("command_result") if isinstance(analysis.get("command_result"), dict) else {}
        assessments = {
            str(item.get("symbol") or "").upper(): item
            for item in command.get("assessments", [])
            if isinstance(item, dict)
        }
        reports = {
            str(item.get("symbol") or "").upper(): item
            for item in analysis.get("holdings", [])
            if isinstance(item, dict)
        }
        created = parse_datetime(analysis.get("generated_at")) or utc_now()
        count = 0
        for action in command.get("action_plan", []):
            if not isinstance(action, dict):
                continue
            symbol = str(action.get("symbol") or "").upper()
            assessment = assessments.get(symbol, {})
            report = reports.get(symbol, {})
            quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
            action_text = str(action.get("action") or action.get("title") or "monitor")
            prediction = self._decision_prediction(action, assessment)
            dedupe_source = (
                f"{created.isoformat()[:16]}|{symbol}|{action_text}|"
                f"{prediction['direction']}|{prediction['horizon_days']}|"
                f"{prediction['return_threshold_percent']}"
            )
            dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
            confidence = assessment.get("confidence") if isinstance(assessment.get("confidence"), dict) else {}
            row = {
                "decision_id": uuid.uuid4().hex,
                "dedupe_key": dedupe_key,
                "created_at": utc_text(created),
                "symbol": symbol,
                "action": action_text,
                "confidence": normalized_probability(confidence.get("score"), 0.5),
                "score": number(assessment.get("score")),
                "risk_level": str(assessment.get("risk_level") or ""),
                "reference_price": number(quote.get("price")) or None,
                "evidence_encrypted": protect_text(_json({"reasons": assessment.get("reasons", []), "risk_flags": assessment.get("risk_flags", []), "source": quote.get("provider"), "as_of": quote.get("as_of"), "prediction": prediction, "market": report.get("market"), "asset_type": report.get("asset_type")})),
                "snapshot_encrypted": protect_text(_json({"decision_brief": command.get("decision_brief"), "portfolio_score": command.get("portfolio_score"), "action": action, "prediction": prediction, "context": {"market": report.get("market"), "asset_type": report.get("asset_type")}})),
                "user_status": "pending",
                "outcome_due_at": utc_text(
                    created + timedelta(days=prediction["horizon_days"])
                ),
                "outcome_encrypted": "",
                "prediction_direction": prediction["direction"],
                "horizon_days": prediction["horizon_days"],
                "return_threshold_percent": prediction["return_threshold_percent"],
                "eligible_for_calibration": int(prediction["eligible_for_calibration"]),
            }
            with self.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO decisions(
                        decision_id, dedupe_key, created_at, symbol, action,
                        confidence, score, risk_level, reference_price,
                        evidence_encrypted, snapshot_encrypted, user_status,
                        outcome_due_at, outcome_encrypted, prediction_direction,
                        horizon_days, return_threshold_percent,
                        eligible_for_calibration
                    ) VALUES(
                        :decision_id, :dedupe_key, :created_at, :symbol, :action,
                        :confidence, :score, :risk_level, :reference_price,
                        :evidence_encrypted, :snapshot_encrypted, :user_status,
                        :outcome_due_at, :outcome_encrypted, :prediction_direction,
                        :horizon_days, :return_threshold_percent,
                        :eligible_for_calibration
                    )
                    """,
                    row,
                )
            count += int(bool(cursor.rowcount))
        return count


    def update_decision_outcomes(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT decision_id, dedupe_key, created_at, symbol, action, confidence, score, risk_level, reference_price, evidence_encrypted, snapshot_encrypted, user_status, outcome_due_at, outcome_encrypted FROM decisions WHERE outcome_encrypted = '' AND outcome_due_at <= ? LIMIT 500",
                (utc_text(now),),
            ).fetchall()
        updated = 0
        for row in rows:
            with self.connect() as connection:
                evaluation_bar = connection.execute(
                    """
                    SELECT close, observed_at, provider, verified
                    FROM prices
                    WHERE symbol = ? AND observed_at >= ?
                    ORDER BY observed_at ASC, verified DESC, provider
                    LIMIT 1
                    """,
                    (str(row["symbol"]), str(row["outcome_due_at"])),
                ).fetchone()
            if evaluation_bar is None:
                continue
            price = number(evaluation_bar["close"], -1)
            reference = number(row["reference_price"], -1)
            if price <= 0 or reference <= 0:
                continue
            return_percent = (price / reference - 1) * 100
            direction = str(row["prediction_direction"] or "abstain")
            threshold = max(0.0, number(row["return_threshold_percent"]))
            eligible = bool(row["eligible_for_calibration"])
            success: bool | None = None
            if eligible and direction == "bullish":
                success = return_percent >= threshold
            elif eligible and direction == "bearish":
                success = return_percent <= -threshold
            elif eligible and direction == "neutral":
                success = abs(return_percent) <= threshold
            outcome = {
                "evaluated_at": utc_text(now),
                "price": price,
                "price_observed_at": str(evaluation_bar["observed_at"]),
                "price_provider": str(evaluation_bar["provider"]),
                "return_percent": rounded(return_percent, 2),
                "direction": direction,
                "horizon_days": int(row["horizon_days"]),
                "return_threshold_percent": threshold,
                "evaluable": success is not None,
                "success": success,
                "evaluation_status": (
                    "evaluated" if success is not None else "not_a_directional_forecast"
                ),
            }
            with self.connect() as connection:
                connection.execute(
                    "UPDATE decisions SET outcome_encrypted = ? WHERE decision_id = ?",
                    (protect_text(_json(outcome)), row["decision_id"]),
                )
            updated += 1
        return updated


    def set_decision_status(self, decision_id: str, status: str) -> bool:
        normalized = status if status in {"pending", "accepted", "rejected", "executed"} else "pending"
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE decisions SET user_status = ? WHERE decision_id = ?",
                (normalized, decision_id),
            )
        return bool(cursor.rowcount)


    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT decision_id, dedupe_key, created_at, symbol, action, confidence, score, risk_level, reference_price, evidence_encrypted, snapshot_encrypted, user_status, outcome_due_at, outcome_encrypted FROM decisions ORDER BY created_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _decoded_json(unprotect_text(item.pop("evidence_encrypted", "")), {})
            item["snapshot"] = _decoded_json(unprotect_text(item.pop("snapshot_encrypted", "")), {})
            item["outcome"] = _decoded_json(unprotect_text(item.pop("outcome_encrypted", "")), {})
            result.append(item)
        return result



__all__ = ['InvestmentAnalyticsStoreQueries']
