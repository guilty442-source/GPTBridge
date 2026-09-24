"""InvestmentAccountService — stable account registry.

Seeded accounts:

- ``CATHAY_TW``    國泰台股 (TWD)
- ``FUBON_US``     富邦美股複委託 (USD)
- ``MUTUAL_FUND``  共同基金 (TWD)
- ``CASH_TWD``     台幣現金
- ``CASH_USD``     美元現金

``account_id`` is the stable identity; labels are mutable display
strings and are NEVER used for identification. Additional platforms can
be registered later without re-keying data.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


SEED_ACCOUNTS: tuple[dict[str, Any], ...] = (
    {"account_id": "CATHAY_TW", "label": "國泰台股",
     "kind": "brokerage", "broker_id": "CATHAY_SECURITIES",
     "market": "tw", "currency": "TWD"},
    {"account_id": "FUBON_US", "label": "富邦美股複委託",
     "kind": "sub-brokerage", "broker_id": "FUBON_SUBBROKERAGE",
     "market": "us", "currency": "USD"},
    {"account_id": "MUTUAL_FUND", "label": "共同基金",
     "kind": "fund", "broker_id": "MUTUAL_FUND_PROVIDER",
     "market": "fund", "currency": "TWD"},
    {"account_id": "CASH_TWD", "label": "台幣現金",
     "kind": "cash", "broker_id": "", "market": "cash",
     "currency": "TWD"},
    {"account_id": "CASH_USD", "label": "美元現金",
     "kind": "cash", "broker_id": "", "market": "cash",
     "currency": "USD"},
)


class InvestmentAccountService:
    """Registry — identity only; balances live in the offline journal."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "investment-accounts.json"
        self._accounts: dict[str, dict[str, Any]] = {}
        self._load()
        for seed in SEED_ACCOUNTS:
            self._accounts.setdefault(seed["account_id"], dict(seed))
        self._persist()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._accounts = {str(k): v for k, v in data.items()
                                  if isinstance(v, dict)}
        except Exception:
            self._accounts = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._accounts, indent=2, ensure_ascii=False),
            encoding="utf-8")

    # ------------------------------------------------------------------
    def register(self, account_id: str, label: str, kind: str, *,
                 broker_id: str = "", market: str = "",
                 currency: str = "") -> dict[str, Any]:
        aid = str(account_id)
        if not aid:
            return {"ok": False, "error_code": "ACCOUNT_ID_REQUIRED"}
        if aid in self._accounts:
            return {"ok": False, "error_code": "ACCOUNT_EXISTS",
                    "account_id": aid}
        self._accounts[aid] = {
            "account_id": aid, "label": str(label or aid),
            "kind": str(kind), "broker_id": str(broker_id),
            "market": str(market), "currency": str(currency).upper(),
            "created_at": time.time(),
        }
        self._persist()
        return {"ok": True, "account": dict(self._accounts[aid])}

    def rename(self, account_id: str, label: str) -> dict[str, Any]:
        acct = self._accounts.get(str(account_id))
        if acct is None:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        acct["label"] = str(label)
        acct["renamed_at"] = time.time()
        self._persist()
        return {"ok": True, "account": dict(acct)}

    def get(self, account_id: str) -> dict[str, Any] | None:
        a = self._accounts.get(str(account_id))
        return dict(a) if a else None

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self._accounts.values()]
