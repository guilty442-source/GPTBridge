"""CashManagementService — TWD/USD cash with segregation.

Per account per currency: ``balance = available + reserved +
in_settlement``. Reservations live in a side journal; settlement
windows come from the market settlement rule (TW T+2, US T+1) applied
to sell transactions — cash from a sale is ``in_settlement`` until the
settlement date, never assumed spendable before then.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SETTLEMENT_DAYS = {"tw": 2, "us": 1, "fund": 0, "cash": 0}


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


class CashManagementService:
    def __init__(self, state_dir: Path, offline: Any) -> None:
        self._offline = offline
        self._dir = state_dir / "asset-cash"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._res_path = self._dir / "reservations.json"
        self._adj_path = self._dir / "events.jsonl"
        self._reservations: dict[str, dict[str, Decimal]] = {}
        self._settling: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._res_path.read_text(encoding="utf-8"))
            for aid, rows in data.items():
                self._reservations[aid] = {
                    k: Decimal(str(v)) for k, v in rows.items()}
        except Exception:
            self._reservations = {}
        if self._adj_path.exists():
            for line in self._adj_path.read_text(
                    encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    if e.get("kind") == "settling":
                        self._settling.append(e)
                except Exception:
                    continue

    def _persist_res(self) -> None:
        self._res_path.write_text(json.dumps(
            {a: {k: str(v) for k, v in rows.items()}
             for a, rows in self._reservations.items()},
            indent=2), encoding="utf-8")

    def _event(self, e: dict[str, Any]) -> None:
        e.setdefault("event_id", f"csh-{uuid.uuid4().hex[:10]}")
        e.setdefault("at", time.time())
        with open(self._adj_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    def deposit(self, account_id: str, currency: str, amount,
                *, source: str = "MANUAL") -> dict[str, Any]:
        return self._move(account_id, currency, amount,
                          kind="deposit", source=source)

    def withdraw(self, account_id: str, currency: str, amount,
                 *, source: str = "MANUAL") -> dict[str, Any]:
        return self._move(account_id, currency, -_d(amount),
                          kind="withdrawal", source=source)

    def adjust(self, account_id: str, currency: str, amount,
               reason: str, *, source: str = "MANUAL") -> dict[str, Any]:
        return self._move(account_id, currency, amount,
                          kind="adjustment", reason=reason,
                          source=source)

    def _move(self, account_id: str, currency: str, amount, *,
              kind: str, reason: str = "", source: str) -> dict[str, Any]:
        acct = self._offline.get(account_id)
        if acct is None:
            return {"ok": False, "error_code": "ACCOUNT_NOT_FOUND"}
        cur = str(currency).upper()
        bal = _d(self._balance(account_id).get(cur, "0"))
        amt = _d(amount)
        if kind in ("withdrawal",) and bal + amt < 0:
            return {"ok": False, "error_code": "INSUFFICIENT_CASH"}
        r = self._offline.set_cash(account_id, cur, str(bal + amt),
                                   source=source)
        if r.get("ok"):
            self._event({"kind": kind, "account_id": account_id,
                         "currency": cur, "amount": str(amt),
                         "reason": reason, "source": source})
        return r

    # ------------------------------------------------------------------
    def reserve(self, account_id: str, currency: str, amount,
                ref: str) -> dict[str, Any]:
        cur = str(currency).upper()
        view = self.balance_view(account_id)
        avail = _d(view["balances"].get(cur, {}).get("available", "0"))
        if _d(amount) > avail:
            return {"ok": False, "error_code": "INSUFFICIENT_AVAILABLE",
                    "available": str(avail)}
        key = f"{cur}:{ref}"
        self._reservations.setdefault(account_id, {})[key] = \
            self._reservations.setdefault(account_id, {}).get(
                key, Decimal(0)) + _d(amount)
        self._persist_res()
        return {"ok": True, "reserved": key}

    def release(self, account_id: str, currency: str, ref: str,
                amount=None) -> dict[str, Any]:
        cur = str(currency).upper()
        key = f"{cur}:{ref}"
        rows = self._reservations.get(account_id, {})
        cur_amt = rows.get(key, Decimal(0))
        take = cur_amt if amount is None else min(_d(amount), cur_amt)
        rows[key] = cur_amt - take
        if rows[key] <= 0:
            rows.pop(key, None)
        self._persist_res()
        return {"ok": True, "released": str(take)}

    def record_settling(self, account_id: str, currency: str, amount,
                        market: str, *, trade_at: float | None = None
                        ) -> dict[str, Any]:
        """Sell proceeds sit in settlement until market T+N."""
        days = SETTLEMENT_DAYS.get(market, 2)
        settle_at = (trade_at or time.time()) + days * 86400
        e = {"kind": "settling", "account_id": account_id,
             "currency": str(currency).upper(), "amount": str(_d(amount)),
             "settle_at": settle_at, "market": market}
        self._settling.append(e)
        self._event(e)
        return {"ok": True, "settle_at": settle_at}

    # ------------------------------------------------------------------
    def _balance(self, account_id: str) -> dict[str, str]:
        return {c["currency"]: c["amount"]
                for c in self._offline.cash(account_id)}

    def balance_view(self, account_id: str) -> dict[str, Any]:
        """balance = available + reserved + in_settlement (all proven)."""
        balances: dict[str, dict[str, str]] = {}
        res = self._reservations.get(account_id, {})
        now = time.time()
        for cur, amt in self._balance(account_id).items():
            reserved = sum(v for k, v in res.items()
                           if k.startswith(f"{cur}:"))
            settling = sum(_d(e["amount"]) for e in self._settling
                           if e["account_id"] == account_id
                           and e["currency"] == cur
                           and e["settle_at"] > now)
            bal = _d(amt)
            balances[cur] = {
                "balance": str(bal),
                "available": str(max(Decimal(0), bal - reserved)),
                "reserved": str(reserved),
                "in_settlement": str(settling),
            }
        for k, v in res.items():
            cur = k.split(":", 1)[0]
            if cur not in balances:
                balances[cur] = {"balance": "0", "available": "0",
                                 "reserved": str(v), "in_settlement": "0"}
        return {"ok": True, "account_id": account_id,
                "balances": balances,
                "note": "unsettled/insufficient data is never assumed "
                        "spendable"}
