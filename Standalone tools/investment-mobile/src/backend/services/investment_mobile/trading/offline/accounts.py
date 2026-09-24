"""OfflineInvestmentAccount — manual/demo/file-imported books.

Three account kinds:

- ``cathay-tw``     國泰台股（TWD 現金 + 台股/ETF 持倉）
- ``fubon-us``      富邦美股複委託（USD 現金 + 美股持倉）
- ``fund``          共同基金帳戶

Sources: ``DEMO`` | ``MANUAL`` | ``FILE_IMPORT``.
``LIVE_BROKER_SYNC`` is NOT a valid source this phase.

Every mutation is an append-only journal entry tagged with source +
timestamp; holdings derive from the journal. Nothing written here is
``broker_confirmed`` — broker state is external truth obtainable only
through a verified adapter (which is disabled this phase).
"""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

SOURCES = frozenset({"DEMO", "MANUAL", "FILE_IMPORT"})
ACCOUNT_KINDS = frozenset({"cathay-tw", "fubon-us", "fund"})
_KIND_META = {
    "cathay-tw": {"broker_id": "CATHAY_SECURITIES", "market": "tw",
                  "currency": "TWD"},
    "fubon-us": {"broker_id": "FUBON_SUBBROKERAGE", "market": "us",
                 "currency": "USD"},
    "fund": {"broker_id": "MUTUAL_FUND_PROVIDER", "market": "fund",
             "currency": "TWD"},
}


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class OfflineAccountService:
    """Append-only offline books under ``runtime/state/offline-accounts``."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "offline-accounts"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._journal_path = self._dir / "journal.jsonl"
        self._accounts: dict[str, dict[str, Any]] = {}
        self._holdings: dict[str, dict[str, dict[str, Any]]] = {}
        self._cash: dict[str, dict[str, dict[str, Any]]] = {}
        self._transactions: dict[str, list[dict[str, Any]]] = {}
        self._dividends: dict[str, list[dict[str, Any]]] = {}
        self._fh = open(self._journal_path, "a", encoding="utf-8")
        self._replay()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _append(self, entry: dict[str, Any]) -> None:
        entry.setdefault("at", time.time())
        entry.setdefault("entry_id", f"off-{uuid.uuid4().hex[:12]}")
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._apply(entry)

    def _replay(self) -> None:
        if not self._journal_path.exists():
            return
        for line in self._journal_path.read_text(
                encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            self._apply(e)

    def _apply(self, e: dict[str, Any]) -> None:
        op = e.get("op")
        aid = e.get("account_id")
        if op == "create_account":
            self._accounts[aid] = e["account"]
            self._holdings.setdefault(aid, {})
            self._cash.setdefault(aid, {})
            self._transactions.setdefault(aid, [])
            self._dividends.setdefault(aid, [])
        elif op == "set_holding":
            self._holdings.setdefault(aid, {})[e["instrument_id"]] = e
        elif op == "remove_holding":
            self._holdings.get(aid, {}).pop(e["instrument_id"], None)
        elif op == "set_cash":
            self._cash.setdefault(aid, {})[e["currency"]] = e
        elif op == "add_transaction":
            self._transactions.setdefault(aid, []).append(e)
        elif op == "add_dividend":
            self._dividends.setdefault(aid, []).append(e)

    # ------------------------------------------------------------------
    def create_account(self, kind: str, label: str, *,
                       source: str = "MANUAL",
                       account_id: str | None = None) -> dict[str, Any]:
        if kind not in ACCOUNT_KINDS:
            return {"ok": False, "error_code": "ACCOUNT_KIND_UNKNOWN",
                    "kinds": sorted(ACCOUNT_KINDS)}
        if source not in SOURCES:
            return {"ok": False, "error_code": "SOURCE_INVALID",
                    "sources": sorted(SOURCES),
                    "note": "LIVE_BROKER_SYNC unsupported this phase"}
        aid = account_id or f"off-{kind}-{uuid.uuid4().hex[:8]}"
        if aid in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_EXISTS",
                    "account_id": aid}
        acct = {
            "account_id": aid, "kind": kind, "label": label,
            "source": source, "created_at": time.time(),
            "broker_confirmed": False, **_KIND_META[kind],
        }
        self._append({"op": "create_account", "account_id": aid,
                      "account": acct, "source": source})
        return {"ok": True, "account": acct}

    def get(self, account_id: str) -> dict[str, Any] | None:
        acct = self._accounts.get(str(account_id))
        return dict(acct) if acct else None

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self._accounts.values()]

    # ------------------------------------------------------------------
    def set_cash(self, account_id: str, currency: str, amount,
                 *, source: str = "MANUAL") -> dict[str, Any]:
        if account_id not in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        if source not in SOURCES:
            return {"ok": False, "error_code": "SOURCE_INVALID"}
        e = {"op": "set_cash", "account_id": account_id,
             "currency": str(currency).upper(),
             "amount": str(_dec(amount)), "source": source,
             "updated_at": time.time(), "broker_confirmed": False}
        self._append(e)
        return {"ok": True, "cash": e}

    def set_holding(self, account_id: str, instrument_id: str,
                    quantity, avg_cost="0", *,
                    source: str = "MANUAL",
                    kind: str | None = None) -> dict[str, Any]:
        acct = self._accounts.get(account_id)
        if acct is None:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        if source not in SOURCES:
            return {"ok": False, "error_code": "SOURCE_INVALID"}
        e = {"op": "set_holding", "account_id": account_id,
             "instrument_id": str(instrument_id),
             "quantity": str(_dec(quantity)),
             "avg_cost": str(_dec(avg_cost)),
             "kind": kind or acct["kind"],
             "market": acct["market"], "currency": acct["currency"],
             "source": source, "updated_at": time.time(),
             "broker_confirmed": False}
        self._append(e)
        return {"ok": True, "holding": e}

    def remove_holding(self, account_id: str, instrument_id: str,
                       *, source: str = "MANUAL") -> dict[str, Any]:
        if account_id not in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        e = {"op": "remove_holding", "account_id": account_id,
             "instrument_id": str(instrument_id), "source": source}
        self._append(e)
        return {"ok": True}

    def add_transaction(self, account_id: str, txn: dict[str, Any],
                        *, source: str = "MANUAL") -> dict[str, Any]:
        if account_id not in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        if source not in SOURCES:
            return {"ok": False, "error_code": "SOURCE_INVALID"}
        e = {"op": "add_transaction", "account_id": account_id,
             "txn_id": f"otx-{uuid.uuid4().hex[:10]}",
             "instrument_id": str(txn.get("instrument_id") or ""),
             "side": str(txn.get("side") or ""),
             "quantity": str(_dec(txn.get("quantity"))),
             "price": str(_dec(txn.get("price"))),
             "fee": str(_dec(txn.get("fee"))),
             "tax": str(_dec(txn.get("tax"))),
             "traded_at": txn.get("traded_at") or time.time(),
             "source": source, "updated_at": time.time(),
             "broker_confirmed": False}
        # typed-ledger fields pass through (transaction_type,
        # settlement_date, gross/net amount, source_id, corrects…)
        for k, v in txn.items():
            if k not in e and k not in ("account_id", "source"):
                e[k] = v
        self._append(e)
        return {"ok": True, "transaction": e}

    def add_dividend(self, account_id: str, div: dict[str, Any],
                     *, source: str = "MANUAL") -> dict[str, Any]:
        if account_id not in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        e = {"op": "add_dividend", "account_id": account_id,
             "dividend_id": f"odv-{uuid.uuid4().hex[:10]}",
             "instrument_id": str(div.get("instrument_id") or ""),
             "amount": str(_dec(div.get("amount"))),
             "currency": str(div.get("currency") or
                             self._accounts[account_id]["currency"]),
             "paid_at": div.get("paid_at") or time.time(),
             "source": source, "updated_at": time.time(),
             "broker_confirmed": False}
        for k, v in div.items():
            if k not in e and k not in ("account_id", "source"):
                e[k] = v
        self._append(e)
        return {"ok": True, "dividend": e}

    # ------------------------------------------------------------------
    def holdings(self, account_id: str) -> list[dict[str, Any]]:
        return [dict(h) for h in self._holdings.get(account_id, {}).values()]

    def cash(self, account_id: str) -> list[dict[str, Any]]:
        return [dict(c) for c in self._cash.get(account_id, {}).values()]

    def transactions(self, account_id: str) -> list[dict[str, Any]]:
        return list(self._transactions.get(account_id, []))

    def dividends(self, account_id: str) -> list[dict[str, Any]]:
        return list(self._dividends.get(account_id, []))

    def account_view(self, account_id: str) -> dict[str, Any]:
        acct = self.get(account_id)
        if acct is None:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        return {"ok": True, "account": acct,
                "cash": self.cash(account_id),
                "holdings": self.holdings(account_id),
                "transactions": self.transactions(account_id),
                "dividends": self.dividends(account_id),
                "broker_confirmed": False,
                "note": "offline data — never broker-verified"}
