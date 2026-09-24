"""broker + account domain — per-broker account isolation.

Brokers: CATHAY_SECURITIES (TW), FUBON_SUBBROKERAGE (US sub-brokerage),
MUTUAL_FUND_PROVIDER (extensible, NOT hardcoded to a specific platform —
manual import supported until a provider API is verified).

Each account isolates: cash, positions, orders, executions, fees,
permissions. PAPER mode uses dedicated simulated accounts
(``paper-*`` ids) — simulated results are always marked ``simulated``
and never masquerade as real trading outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import Account, BrokerId, CashBalance


_DEFAULT_ACCOUNTS = (
    Account(
        account_id="cathay-tw-main",
        broker_id=BrokerId.CATHAY_SECURITIES.value,
        market="tw",
        currency="TWD",
        permissions=["analysis", "shadow", "paper"],
        label="國泰證券台股帳戶",
    ),
    Account(
        account_id="fubon-us-main",
        broker_id=BrokerId.FUBON_SUBBROKERAGE.value,
        market="us",
        currency="USD",
        permissions=["analysis", "shadow", "paper"],
        label="富邦證券美股複委託帳戶",
    ),
    Account(
        account_id="fund-provider-generic",
        broker_id=BrokerId.MUTUAL_FUND_PROVIDER.value,
        market="fund",
        currency="TWD",
        permissions=["analysis", "shadow", "paper", "manual-import"],
        label="共同基金平台（待接入）",
    ),
)


class AccountRegistry:
    """Accounts + cash balances; paper accounts are separate ids."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir
        self._accounts_path = state_dir / "accounts.json"
        self._cash_path = state_dir / "cash-balances.json"
        self._accounts: dict[str, Account] = {}
        self._cash: dict[tuple[str, str], CashBalance] = {}
        self._load()

    def _load(self) -> None:
        try:
            rows = json.loads(self._accounts_path.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                self._accounts[row["account_id"]] = Account(**row)
        except Exception:
            pass
        for account in _DEFAULT_ACCOUNTS:
            self._accounts.setdefault(account.account_id, account)
        try:
            rows = json.loads(self._cash_path.read_text(encoding="utf-8"))
            for row in rows if isinstance(rows, list) else []:
                bal = CashBalance(**row)
                self._cash[(bal.account_id, bal.currency)] = bal
        except Exception:
            pass

    def _persist(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        for path, rows in (
            (self._accounts_path, [a.to_dict() for a in self._accounts.values()]),
            (self._cash_path, [c.to_dict() for c in self._cash.values()]),
        ):
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)

    # ------------------------------------------------------------------
    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(str(account_id))

    def for_market(self, market: str, *, paper: bool = False) -> Account | None:
        if paper:
            return self.paper_account(market)
        for account in self._accounts.values():
            if account.market == market and not account.account_id.startswith("paper-"):
                return account
        return None

    def paper_account(self, market: str) -> Account:
        """Dedicated simulated account — never a real broker account."""
        account_id = f"paper-{market}"
        account = self._accounts.get(account_id)
        if account is None:
            account = Account(
                account_id=account_id,
                broker_id="PAPER",
                market=market,
                currency="TWD" if market != "us" else "USD",
                permissions=["paper"],
                label=f"模擬帳戶（{market}）",
            )
            self._accounts[account_id] = account
            self._persist()
        return account

    # ------------------------------------------------------------------
    def cash(self, account_id: str, currency: str) -> CashBalance:
        key = (str(account_id), str(currency))
        bal = self._cash.get(key)
        if bal is None:
            bal = CashBalance(
                account_id=key[0], currency=key[1],
                simulated=key[0].startswith("paper-"),
            )
            self._cash[key] = bal
            self._persist()
        return bal

    def apply_execution_cash(
        self, account_id: str, currency: str, delta: float
    ) -> CashBalance:
        bal = self.cash(account_id, currency)
        bal.available += float(delta)
        bal.updated_at = __import__("time").time()
        self._persist()
        return bal

    def set_cash(self, account_id: str, currency: str, available: float) -> CashBalance:
        bal = self.cash(account_id, currency)
        bal.available = float(available)
        self._persist()
        return bal

    # ------------------------------------------------------------------
    def list_accounts(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self._accounts.values()]

    def list_cash(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._cash.values()]
