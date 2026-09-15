from __future__ import annotations

import hashlib
import sqlite3
import uuid
from typing import Any

from .analytics_common import (
    OPENING_BALANCE_PREFIX,
    RECONCILIATION_PREFIX,
    _json,
    number,
    parse_datetime,
    protect_text,
    rounded,
    unprotect_text,
    utc_now,
    utc_text,
)

_TRANSACTION_UPSERT_SQL = """
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
    """


class AnalyticsStoreTransactionsMixin:
    """Ledger transaction writes, tombstones, and opening-balance sync."""

    def add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._normalized_transaction(payload)
        with self.connect() as connection:
            connection.execute(_TRANSACTION_UPSERT_SQL, row)
        public = dict(row)
        encrypted_note = str(public.pop("note_encrypted", ""))
        public["note"] = unprotect_text(encrypted_note)
        return public

    def _normalized_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        return {
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
            row = self._active_transaction_row(connection, normalized)
            if row is None:
                return False
            cursor = self._tombstone_transaction(
                connection,
                normalized,
                reason=reason,
                deleted_at=deleted_at,
                audit_id=audit_id,
            )
            audit_details = {
                "transaction_id": normalized,
                "reason": str(reason or "user_requested"),
                "preserved_row": dict(row),
                "physical_delete": False,
            }
            self._insert_tombstone_audit(
                connection,
                audit_id=audit_id,
                occurred_at=deleted_at,
                details=audit_details,
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

    @staticmethod
    def _tombstone_transaction(
        connection: sqlite3.Connection,
        transaction_id: str,
        *,
        reason: str,
        deleted_at: str,
        audit_id: str,
    ) -> sqlite3.Cursor:
        return connection.execute(
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
                transaction_id,
            ),
        )

    @staticmethod
    def _active_transaction_row(
        connection: sqlite3.Connection,
        transaction_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT transaction_id, occurred_at, symbol, market, asset_type, side, quantity, price, currency, fee, tax, note_encrypted, created_at, deleted_at, delete_reason_encrypted, delete_audit_id FROM transactions
            WHERE transaction_id = ? AND deleted_at = ''
            """,
            (transaction_id,),
        ).fetchone()

    @staticmethod
    def _insert_tombstone_audit(
        connection: sqlite3.Connection,
        *,
        audit_id: str,
        occurred_at: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log(
                audit_id, occurred_at, action, severity, details_encrypted
            ) VALUES(?, ?, 'transaction_tombstoned', 'warning', ?)
            """,
            (
                audit_id,
                occurred_at,
                protect_text(
                    _json(
                        details
                    )
                ),
            ),
        )

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
        rows, active_count, uncovered_symbols = self._opening_balance_rows(
            holdings,
            opening_at,
        )
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
        stale_audit_record = self._replace_opening_transactions(rows, metadata)
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

    def _opening_balance_rows(
        self,
        holdings: list[dict[str, Any]],
        opening_at: Any,
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
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
        return rows, active_count, uncovered_symbols

    def _replace_opening_transactions(
        self,
        rows: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]] | None:
        desired_ids = {row["transaction_id"] for row in rows}
        stale_audit_record: tuple[str, str, dict[str, Any]] | None = None
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
            stale_audit_record = self._tombstone_stale_opening_rows(
                connection,
                desired_ids,
            )
            if rows:
                connection.executemany(_TRANSACTION_UPSERT_SQL, rows)
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                ("opening_ledger", _json(metadata), utc_text()),
            )
        return stale_audit_record

    def _tombstone_stale_opening_rows(
        self,
        connection: sqlite3.Connection,
        desired_ids: set[str],
    ) -> tuple[str, str, dict[str, Any]] | None:
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
        if not stale_ids:
            return None
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
        self._insert_opening_tombstone_audit(
            connection,
            audit_id=stale_audit_id,
            occurred_at=stale_at,
            details=stale_details,
        )
        return (stale_audit_id, stale_at, stale_details)

    @staticmethod
    def _insert_opening_tombstone_audit(
        connection: sqlite3.Connection,
        *,
        audit_id: str,
        occurred_at: str,
        details: dict[str, Any],
    ) -> None:
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
                audit_id,
                occurred_at,
                protect_text(
                    _json(
                        details
                    )
                ),
            ),
        )


__all__ = ['AnalyticsStoreTransactionsMixin']
