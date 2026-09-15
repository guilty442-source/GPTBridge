from __future__ import annotations

import hashlib
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


_SELECT_LIVE_TRANSACTION_SQL = """
SELECT transaction_id, occurred_at, symbol, market, asset_type, side, quantity, price, currency, fee, tax, note_encrypted, created_at, deleted_at, delete_reason_encrypted, delete_audit_id FROM transactions
WHERE transaction_id = ? AND deleted_at = ''
"""

_TOMBSTONE_TRANSACTION_SQL = """
UPDATE transactions
SET deleted_at=?, delete_reason_encrypted=?,
    delete_audit_id=?
WHERE transaction_id=? AND deleted_at=''
"""

_INSERT_TRANSACTION_TOMBSTONE_AUDIT_SQL = """
INSERT INTO audit_log(
    audit_id, occurred_at, action, severity, details_encrypted
) VALUES(?, ?, 'transaction_tombstoned', 'warning', ?)
"""

_COUNT_CONFIRMED_TRANSACTIONS_SQL = """
SELECT COUNT(*) FROM transactions
WHERE transaction_id NOT LIKE ? AND deleted_at = ''
"""

_SELECT_LIVE_OPENING_IDS_SQL = """
SELECT transaction_id FROM transactions
WHERE transaction_id LIKE ? AND deleted_at = ''
"""

_TOMBSTONE_STALE_OPENING_SQL = """
UPDATE transactions
SET deleted_at=?,
    delete_reason_encrypted=?,
    delete_audit_id=?
WHERE transaction_id=? AND deleted_at=''
"""

_INSERT_OPENING_TOMBSTONE_AUDIT_SQL = """
INSERT INTO audit_log(
    audit_id, occurred_at, action, severity,
    details_encrypted
) VALUES(
    ?, ?, 'opening_transactions_tombstoned',
    'warning', ?
)
"""

_UPSERT_OPENING_ROW_SQL = """
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


class TransactionsMixin:
    """Transaction CRUD and opening balance sync methods."""

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
        row = self._transaction_row(
            payload, side, symbol, quantity, price, occurred, transaction_id
        )
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

    def _transaction_row(
        self,
        payload: dict[str, Any],
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        occurred: Any,
        transaction_id: str,
    ) -> dict[str, Any]:
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
            outcome = self._tombstone_transaction(
                connection, normalized, reason, deleted_at, audit_id
            )
            if outcome is None:
                return False
            cursor, audit_details = outcome
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

    def _tombstone_transaction(
        self,
        connection: Any,
        normalized: str,
        reason: str,
        deleted_at: str,
        audit_id: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        row = connection.execute(
            _SELECT_LIVE_TRANSACTION_SQL,
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            _TOMBSTONE_TRANSACTION_SQL,
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
            _INSERT_TRANSACTION_TOMBSTONE_AUDIT_SQL,
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
        return cursor, audit_details

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
            holdings, opening_at
        )
        desired_ids = {row["transaction_id"] for row in rows}
        metadata = self._opening_balance_metadata(
            opening_at, active_count, rows, uncovered_symbols
        )
        with self.connect() as connection:
            self._assert_no_confirmed_transactions(connection)
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    _SELECT_LIVE_OPENING_IDS_SQL,
                    (f"{OPENING_BALANCE_PREFIX}%",),
                ).fetchall()
            }
            stale_audit_record = self._tombstone_stale_opening_rows(
                connection, existing_ids - desired_ids
            )
            self._upsert_opening_rows(connection, rows)
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
            row = self._opening_balance_row(holding, index, quantity, opening_at)
            if row is None:
                uncovered_symbols.append(
                    str(holding.get("symbol") or "").strip().upper()
                    or f"ROW-{index + 1}"
                )
                continue
            rows.append(row)
        return rows, active_count, uncovered_symbols

    def _opening_balance_row(
        self,
        holding: dict[str, Any],
        index: int,
        quantity: float,
        opening_at: Any,
    ) -> dict[str, Any] | None:
        symbol = str(holding.get("symbol") or "").strip().upper()
        principal_twd = number(holding.get("principal_twd"))
        holding_currency = str(holding.get("currency") or "TWD").strip().upper()
        if principal_twd <= 0 and holding_currency == "TWD":
            principal_twd = number(holding.get("principal_amount"))
            if principal_twd <= 0:
                principal_twd = number(holding.get("average_cost")) * quantity
        if not symbol or principal_twd <= 0:
            return None
        identity = str(
            holding.get("holding_id")
            or f"{holding.get('market')}|{symbol}|{index}"
        )
        transaction_id = OPENING_BALANCE_PREFIX + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:24]
        return {
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

    def _tombstone_stale_opening_rows(
        self,
        connection: Any,
        stale_ids: set[str],
    ) -> tuple[str, str, dict[str, Any]] | None:
        if not stale_ids:
            return None
        stale_audit_id = uuid.uuid4().hex
        stale_at = utc_text()
        connection.executemany(
            _TOMBSTONE_STALE_OPENING_SQL,
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
        connection.execute(
            _INSERT_OPENING_TOMBSTONE_AUDIT_SQL,
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
        return stale_audit_id, stale_at, stale_details

    def _assert_no_confirmed_transactions(self, connection: Any) -> None:
        confirmed_count = int(
            connection.execute(
                _COUNT_CONFIRMED_TRANSACTIONS_SQL,
                (f"{OPENING_BALANCE_PREFIX}%",),
            ).fetchone()[0]
        )
        if confirmed_count > 0:
            raise ValueError(
                "confirmed transactions already exist; import broker history instead of rebuilding the opening balance"
            )

    def _opening_balance_metadata(
        self,
        opening_at: Any,
        active_count: int,
        rows: list[dict[str, Any]],
        uncovered_symbols: list[str],
    ) -> dict[str, Any]:
        return {
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

    def _upsert_opening_rows(
        self, connection: Any, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        connection.executemany(
            _UPSERT_OPENING_ROW_SQL,
            rows,
        )
