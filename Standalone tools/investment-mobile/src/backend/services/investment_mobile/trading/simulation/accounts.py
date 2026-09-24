"""PaperAccountService + PaperCashLedger.

Every cash movement is a ledger entry — balances are derived, never
mutated in place. Sell proceeds land as UNSETTLED and move to AVAILABLE
only after the market's settlement lag (TW T+2 / US T+1). Paper accounts
are separate ids (paper-*) — never confused with real broker accounts.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import CashState, PaperAccount

_ENTRY_KINDS = frozenset({
    "initial", "deposit", "withdraw", "buy_debit", "sell_credit",
    "fee", "tax", "dividend", "adjustment", "settle", "reserve",
    "release", "transfer", "fx_convert",
})

_SEED_ACCOUNTS = [
    ("paper-cathay-tw", "CATHAY_TW_PAPER", "TAIWAN_EQUITY", "TWD"),
    ("paper-fubon-us", "FUBON_US_PAPER", "US_EQUITY", "USD"),
]


class PaperAccountService:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "paper_accounts.json"
        self._ledger_path = self._dir / "paper_cash_ledger.jsonl"
        self._accounts: dict[str, PaperAccount] = {}
        self._entries: list[dict[str, Any]] = self._read_ledger()
        self._ledger_fp: Any | None = None
        self._load()
        for aid, name, market, ccy in _SEED_ACCOUNTS:
            if aid not in self._accounts:
                self._accounts[aid] = PaperAccount(
                    account_id=aid, account_name=name, market=market,
                    base_currency=ccy, initial_capital=Decimal("0"))
        self._persist()

    def close(self) -> None:
        if self._ledger_fp is not None:
            self._ledger_fp.close()
            self._ledger_fp = None

    # ------------------------------------------------------------------
    def create(self, account: PaperAccount) -> dict[str, Any]:
        if not account.account_id.startswith("paper-"):
            return {"ok": False, "error_code": "PAPER_ID_REQUIRED"}
        self._accounts[account.account_id] = account
        self._persist()
        if account.initial_capital > 0:
            self.post(account.account_id, "initial",
                      account.initial_capital, CashState.AVAILABLE,
                      ref="init")
        return {"ok": True, "account": account.to_dict()}

    def get(self, account_id: str) -> dict[str, Any] | None:
        a = self._accounts.get(account_id)
        return a.to_dict() if a else None

    def list(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self._accounts.values()]

    def for_market(self, market: str) -> PaperAccount | None:
        return next((a for a in self._accounts.values()
                     if a.market == market), None)

    # ------------------------------------------------------------------
    def post(self, account_id: str, kind: str, amount,
             state: str, ref: str = "",
             settle_on: str = "") -> dict[str, Any]:
        if kind not in _ENTRY_KINDS:
            return {"ok": False, "error_code": "ENTRY_KIND_UNKNOWN"}
        if account_id not in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        row = {
            "entry_id": f"pled-{uuid.uuid4().hex[:12]}",
            "account_id": account_id, "kind": kind,
            "amount": str(Decimal(str(amount))), "state": state,
            "ref": ref, "settle_on": settle_on, "at": time.time(),
            "simulated": True,
        }
        if self._ledger_fp is None:
            self._ledger_fp = self._ledger_path.open("a",
                                                   encoding="utf-8")
        self._ledger_fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._ledger_fp.flush()
        self._entries.append(row)
        return {"ok": True, "entry": row}

    def ledger(self, account_id: str | None = None,
               limit: int = 500) -> list[dict[str, Any]]:
        rows = self._entries
        if account_id:
            rows = [r for r in rows if r["account_id"] == account_id]
        return rows[-limit:]

    def _read_ledger(self) -> list[dict[str, Any]]:
        if not self._ledger_path.exists():
            return []
        out = []
        for l in self._ledger_path.read_text("utf-8").splitlines():
            try:
                out.append(json.loads(l))
            except ValueError:
                continue
        return out

    def cash(self, account_id: str,
             on_date: date | None = None) -> dict[str, Any]:
        """Derived balances — sell proceeds settle on their settle_on."""
        today = on_date or date.today()
        bal = {CashState.AVAILABLE: Decimal("0"),
               CashState.RESERVED: Decimal("0"),
               CashState.UNSETTLED: Decimal("0")}
        for e in self.ledger(account_id, limit=10_000):
            amt = Decimal(e["amount"])
            state = e["state"]
            if state == CashState.UNSETTLED:
                settle_on = e.get("settle_on") or ""
                if settle_on and settle_on <= today.isoformat():
                    state = CashState.AVAILABLE  # settled by now
            bal[state] = bal.get(state, Decimal(0)) + amt
        return {
            "ok": True, "account_id": account_id,
            "available": str(bal.get(CashState.AVAILABLE, 0)),
            "reserved": str(bal.get(CashState.RESERVED, 0)),
            "unsettled": str(bal.get(CashState.UNSETTLED, 0)),
            "settled_total": str(bal.get(CashState.AVAILABLE, 0)),
            "simulated": True,
        }

    def reserve(self, account_id: str, amount, ref: str) -> dict[str, Any]:
        cash = self.cash(account_id)
        if Decimal(cash["available"]) < Decimal(str(amount)):
            return {"ok": False, "error_code": "INSUFFICIENT_CASH"}
        self.post(account_id, "reserve", -Decimal(str(amount)),
                  CashState.AVAILABLE, ref=ref)
        self.post(account_id, "reserve", Decimal(str(amount)),
                  CashState.RESERVED, ref=ref)
        return {"ok": True}

    def release(self, account_id: str, amount, ref: str) -> None:
        self.post(account_id, "release", -Decimal(str(amount)),
                  CashState.RESERVED, ref=ref)
        self.post(account_id, "release", Decimal(str(amount)),
                  CashState.AVAILABLE, ref=ref)

    def release_all(self, account_id: str, ref: str) -> Decimal:
        """Release the net remaining RESERVED balance tagged `ref`
        (reserve posts +RESERVED, release posts -RESERVED — sum legs)."""
        net = Decimal("0")
        for e in self.ledger(account_id, limit=10_000):
            if e["ref"] != ref or e["kind"] not in ("reserve", "release"):
                continue
            if e["state"] == CashState.RESERVED:
                net += Decimal(e["amount"])
        if net > 0:
            self.release(account_id, net, ref)
        return net

    def deposit(self, account_id: str, amount, ref: str = "deposit"
                ) -> dict[str, Any]:
        return self.post(account_id, "deposit", Decimal(str(amount)),
                         CashState.AVAILABLE, ref=ref)

    def withdraw(self, account_id: str, amount, ref: str = "withdraw"
                 ) -> dict[str, Any]:
        amt = Decimal(str(amount))
        cash = self.cash(account_id)
        if Decimal(cash["available"]) < amt:
            return {"ok": False, "error_code": "INSUFFICIENT_CASH"}
        return self.post(account_id, "withdraw", -amt,
                         CashState.AVAILABLE, ref=ref)

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text("utf-8"))
        except ValueError:
            return
        for r in rows:
            self._accounts[r["account_id"]] = PaperAccount(
                account_id=r["account_id"], account_name=r["account_name"],
                market=r["market"], base_currency=r["base_currency"],
                initial_capital=Decimal(str(r["initial_capital"])),
                status=r.get("status", "ACTIVE"),
                created_at=float(r.get("created_at") or time.time()))

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            [a.to_dict() for a in self._accounts.values()],
            ensure_ascii=False, indent=1), "utf-8")
