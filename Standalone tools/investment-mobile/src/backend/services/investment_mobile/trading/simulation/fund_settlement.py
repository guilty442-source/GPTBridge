"""PaperFundSettlementService — fund PAPER orders settle on the NAV cycle.

Fund orders never take the equity candle-fill path. A subscribe/redeem
application enters the legal ``FundTransaction`` state machine in a
SIMULATION-ONLY journal (``fund-paper/fund-transactions.jsonl`` —
physically separate from the authoritative fund transaction ledger, so
paper activity can never contaminate manually imported holdings).

Pricing: the first published NAV whose ``nav_date >= application_date``
is the deal price ("next published NAV", per the MUTUAL_FUND_PROVIDER
capability profile). Until such a NAV exists the application waits in
PRICING_PENDING — an estimated NAV is never substituted. Cash moves only
through the paper account ledger; units live in the paper journal and
are valued via published NAV.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..fund.contracts import (
    FundTransaction, FundTransactionType, FundTxnStatus, FeeKind,
)
from ..fund.cost import FundCostBasisService
from ..fund.transactions import FundTransactionService

_BUY_SIDES = frozenset({"buy", "subscribe"})
_SELL_SIDES = frozenset({"sell", "redeem"})
_BUY_TXNS = frozenset({
    FundTransactionType.SUBSCRIBE.value, FundTransactionType.ADD.value,
    FundTransactionType.RECURRING.value,
    FundTransactionType.REINVEST.value,
})
_SELL_TXNS = frozenset({
    FundTransactionType.REDEEM.value,
    FundTransactionType.PARTIAL_REDEEM.value,
})
_BUY_FEE_KINDS = (
    FeeKind.SUBSCRIPTION.value, FeeKind.PLATFORM.value,
    FeeKind.FX.value, FeeKind.OTHER.value,
)
_SELL_FEE_KINDS = (
    FeeKind.REDEMPTION.value, FeeKind.SHORT_TERM.value,
    FeeKind.PLATFORM.value, FeeKind.FX.value, FeeKind.OTHER.value,
)


def _meta(txn: dict[str, Any]) -> dict[str, Any]:
    try:
        m = json.loads(txn.get("note") or "{}")
        return m if isinstance(m, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class PaperFundSettlementService:
    DEFAULT_SETTLE_LAG_DAYS = 1
    SOURCE_ID = "sim:paper"

    def __init__(self, state_dir: Path, *, nav: Any, fees: Any,
                 accounts: Any, risk: Any, recovery: Any) -> None:
        self._nav = nav
        self._fees = fees
        self._accounts = accounts
        self._risk = risk
        self._recovery = recovery
        self.txns = FundTransactionService(
            Path(state_dir) / "fund-paper")
        self._basis = FundCostBasisService(self.txns, nav)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse(instrument_id: str) -> tuple[str, str, str] | None:
        parts = instrument_id.split(":")
        if len(parts) < 4 or parts[0].lower() != "fund":
            return None
        return parts[1].upper(), parts[2].upper(), parts[3].upper()

    def _held_units(self, account_id: str, fund_id: str,
                    share_class_id: str) -> Decimal:
        b = self._basis.basis(account_id, fund_id, share_class_id)
        return Decimal(str(b.get("units") or 0))

    def _committed_units(self, account_id: str, fund_id: str,
                         share_class_id: str) -> Decimal:
        """Units already claimed by non-final sell applications."""
        total = Decimal("0")
        for t in self.txns.list(account_id):
            if (t["fund_id"] == fund_id
                    and t["share_class_id"] == share_class_id
                    and t["transaction_type"] in _SELL_TXNS
                    and t["status"] not in (
                        FundTxnStatus.SETTLED.value,
                        FundTxnStatus.REJECTED.value,
                        FundTxnStatus.CANCELLED.value)):
                total += Decimal(str(t["units"]))
        return total

    def _risk_scope(self, account_id: str) -> tuple[
            list[dict[str, Any]], list[dict[str, Any]]]:
        positions = [
            {"instrument_id": p["instrument_id"], "quantity": p["units"]}
            for p in self.positions(account_id)]
        open_orders = [
            {"instrument_id": t["instrument_id"],
             "side": ("subscribe"
                      if t["transaction_type"] in _BUY_TXNS else "redeem")}
            for t in self.transactions(account_id)
            if t["status"] not in (
                FundTxnStatus.SETTLED.value,
                FundTxnStatus.REJECTED.value,
                FundTxnStatus.CANCELLED.value)]
        return positions, open_orders

    # ------------------------------------------------------------------
    def submit(self, payload: dict[str, Any], account: dict[str, Any],
               *, strategy_run: dict[str, Any] | None = None
               ) -> dict[str, Any]:
        """NAV-cycle application — returns an accepted application, never
        a same-instant fill."""
        iid = str(payload.get("instrument_id") or "")
        parsed = self._parse(iid)
        if parsed is None:
            return {"ok": False, "error_code": "INVALID_INSTRUMENT",
                    "note": "基金商品代碼格式 fund:{id}:{class}:{currency}"}
        fund_id, share_class_id, ccy = parsed
        account_id = account["account_id"]
        side = str(payload.get("side") or "").lower()
        if side not in _BUY_SIDES | _SELL_SIDES:
            return {"ok": False, "error_code": "SIDE_UNKNOWN"}

        nav_res = self._nav.latest_published(fund_id, share_class_id)
        if not nav_res.get("ok"):
            return {"ok": False, "error_code": "NO_NAV",
                    "note": "無已公告淨值——申購/贖回不得以估計值計價"}
        if nav_res.get("stale"):
            return {"ok": False, "error_code": "STALE_NAV",
                    "nav_date": nav_res["nav"]["nav_date"],
                    "note": "最新公告淨值已過期——先匯入新淨值"}
        nav = Decimal(str(nav_res["nav"]["nav"]))
        nav_date = nav_res["nav"]["nav_date"]

        cid = str(payload.get("client_order_id") or "")
        if cid:
            for t in self.txns.list(account_id):
                if _meta(t).get("cid") == cid:
                    return {"ok": True, "dedup": True, "transaction": t}

        today = date.today()
        meta = {"cid": cid,
                "settle_lag_days": int(payload.get(
                    "settle_lag_days", self.DEFAULT_SETTLE_LAG_DAYS))}

        if side in _BUY_SIDES:
            amount = Decimal(str(
                payload.get("amount") or payload.get("quantity") or 0))
            if amount <= 0:
                return {"ok": False, "error_code": "AMOUNT_REQUIRED",
                        "note": "基金申購以金額下單（amount）"}
            txn_type = FundTransactionType.SUBSCRIBE.value
            units = Decimal("0")
            fee_q = self._fees.transaction_fees(
                fund_id, share_class_id, _BUY_FEE_KINDS, amount,
                currency=ccy)
            fees = Decimal(str(fee_q["investor_paid_total"]))
            qty_est = amount / nav if nav > 0 else Decimal("0")
            cash = self._accounts.cash(account_id)
        else:
            units = Decimal(str(payload.get("quantity") or 0))
            if units <= 0:
                return {"ok": False, "error_code": "UNITS_REQUIRED",
                        "note": "基金贖回以單位數下單（quantity）"}
            held = (self._held_units(account_id, fund_id, share_class_id)
                    - self._committed_units(
                        account_id, fund_id, share_class_id))
            if units > held:
                return {"ok": False, "error_code": "INSUFFICIENT_UNITS",
                        "held_available": str(held)}
            txn_type = (FundTransactionType.PARTIAL_REDEEM.value
                        if units < held else FundTransactionType.REDEEM.value)
            amount = Decimal("0")
            fees = Decimal("0")
            qty_est = units
            cash = self._accounts.cash(account_id)

        positions, open_orders = self._risk_scope(account_id)
        # published NAV is the risk sizing basis only — never the deal
        # price; NAV staleness is gated above via the service's stale flag
        decision = self._risk.evaluate(
            account_id=account_id, instrument_id=iid,
            side="subscribe" if side in _BUY_SIDES else "redeem",
            quantity=qty_est, price=nav, strategy_run=strategy_run,
            positions=positions, open_orders=open_orders,
            cash_available=Decimal(cash["available"]),
            equity=(Decimal(cash["available"])
                    + Decimal(cash["reserved"])
                    + Decimal(cash["unsettled"])),
            peak_equity=(Decimal(cash["available"])
                         + Decimal(cash["reserved"])
                         + Decimal(cash["unsettled"])),
            daily_pnl=Decimal("0"), quote_age_s=0.0)
        if decision["outcome"] != "ALLOW":
            return {"ok": False,
                    "error_code": "RISK_" + decision["outcome"],
                    "decision": decision}

        txn = FundTransaction(
            account_id=account_id, fund_id=fund_id,
            share_class_id=share_class_id,
            transaction_type=txn_type, amount=amount, units=units,
            fees=fees, currency=ccy or nav_res["nav"]["currency"],
            status=FundTxnStatus.DRAFT.value,
            application_date=today, source_id=self.SOURCE_ID,
            note=json.dumps(meta, ensure_ascii=False))
        res = self.txns.create(txn)
        if not res.get("ok"):
            return res
        tid = txn.transaction_id

        if side in _BUY_SIDES:
            held_cash = self._accounts.reserve(
                account_id, amount + fees, ref=tid)
            if not held_cash.get("ok"):
                self.txns.transition(tid, FundTxnStatus.CANCELLED.value)
                return {"ok": False, "error_code": "INSUFFICIENT_CASH",
                        "transaction": res["transaction"]}

        for target in (FundTxnStatus.SUBMITTED.value,
                       FundTxnStatus.ACCEPTED.value):
            step = self.txns.transition(tid, target)
            if not step.get("ok"):
                self._accounts.release_all(account_id, tid)
                return step

        self._recovery.record_event("paper_fund_txn", {
            "transaction_id": tid, "account_id": account_id,
            "transaction_type": txn_type})
        return {
            "ok": True, "transaction": step["transaction"],
            "simulated": True, "pricing_basis": "next_published_nav",
            "reference_nav": {"nav": str(nav), "nav_date": nav_date},
            "note": "已受理——以次一公告淨值計價，非即時成交",
        }

    # ------------------------------------------------------------------
    def advance(self) -> dict[str, Any]:
        """Walk paper applications forward along the legal NAV-cycle
        states. Called on maintenance ticks and on every new published
        NAV (the pricing trigger)."""
        priced = settled = waiting = 0
        results: list[dict[str, Any]] = []
        for t in self.txns.list():
            tid = t["transaction_id"]
            cur = t
            status = cur["status"]
            if status == FundTxnStatus.SUBMITTED.value:
                step = self.txns.transition(
                    tid, FundTxnStatus.ACCEPTED.value)
                if step.get("ok"):
                    cur = step["transaction"]
                    status = FundTxnStatus.ACCEPTED.value
            if status in (FundTxnStatus.ACCEPTED.value,
                          FundTxnStatus.PRICING_PENDING.value):
                app_date = (date.fromisoformat(str(cur["application_date"]))
                            if cur.get("application_date") else date.today())
                navs = [n for n in self._nav.history(
                            cur["fund_id"], cur["share_class_id"],
                            start=app_date)
                        if n.data_status == "ok"]
                if navs:
                    n = navs[0]
                    patch: dict[str, Any] = {
                        "confirmed_nav": n.nav, "pricing_date": n.nav_date}
                    if cur["transaction_type"] in _SELL_TXNS:
                        gross = Decimal(str(cur["units"])) * n.nav
                        fee_q = self._fees.transaction_fees(
                            cur["fund_id"], cur["share_class_id"],
                            _SELL_FEE_KINDS, gross,
                            currency=cur.get("currency") or "")
                        patch["amount"] = gross
                        patch["fees"] = Decimal(
                            str(fee_q["investor_paid_total"]))
                    step = self.txns.transition(
                        tid, FundTxnStatus.PRICED.value, **patch)
                    if step.get("ok"):
                        cur = step["transaction"]
                        priced += 1
                        status = FundTxnStatus.PRICED.value
                else:
                    waiting += 1
                    if status == FundTxnStatus.ACCEPTED.value:
                        step = self.txns.transition(
                            tid, FundTxnStatus.PRICING_PENDING.value)
                        if step.get("ok"):
                            cur = step["transaction"]
                            status = FundTxnStatus.PRICING_PENDING.value
            if status == FundTxnStatus.PRICED.value:
                lag = _meta(cur).get(
                    "settle_lag_days", self.DEFAULT_SETTLE_LAG_DAYS)
                pricing_date = (
                    date.fromisoformat(str(cur["pricing_date"]))
                    if cur.get("pricing_date") else date.today())
                step = self.txns.transition(
                    tid, FundTxnStatus.SETTLEMENT_PENDING.value,
                    settlement_date=pricing_date + timedelta(days=int(lag)))
                if step.get("ok"):
                    cur = step["transaction"]
                    status = FundTxnStatus.SETTLEMENT_PENDING.value
            if status == FundTxnStatus.SETTLEMENT_PENDING.value:
                sdate = (date.fromisoformat(str(cur["settlement_date"]))
                         if cur.get("settlement_date") else None)
                if sdate is not None and date.today() >= sdate:
                    res = self._settle(cur)
                    if res.get("ok"):
                        cur = res["transaction"]
                        settled += 1
                        status = FundTxnStatus.SETTLED.value
            results.append({"transaction_id": tid, "status": status})
        return {"ok": True, "priced": priced, "settled": settled,
                "waiting_nav": waiting, "transactions": results}

    def _settle(self, t: dict[str, Any]) -> dict[str, Any]:
        """SETTLEMENT_PENDING → SETTLED + cash posting (paper ledger)."""
        tid = t["transaction_id"]
        account_id = t["account_id"]
        step = self.txns.transition(tid, FundTxnStatus.SETTLED.value)
        if not step.get("ok"):
            return step
        done = step["transaction"]
        amount = Decimal(str(done["amount"]))
        fees = Decimal(str(done["fees"]))
        if done["transaction_type"] in _BUY_TXNS:
            self._accounts.release_all(account_id, tid)
            self._accounts.post(
                account_id, "buy_debit", -(amount + fees),
                "AVAILABLE", ref=tid)
        else:
            self._accounts.post(
                account_id, "sell_credit", amount - fees,
                "UNSETTLED", ref=tid,
                settle_on=str(done.get("settlement_date") or ""))
        self._recovery.record_event("paper_fund_settled", {
            "transaction_id": tid, "account_id": account_id})
        return {"ok": True, "transaction": done}

    # ------------------------------------------------------------------
    def transactions(self, account_id: str | None = None
                     ) -> list[dict[str, Any]]:
        out = []
        for t in self.txns.list(account_id):
            fid, cls = t["fund_id"], t["share_class_id"]
            t["instrument_id"] = (
                f"fund:{fid}:{cls}:{t.get('currency') or ''}")
            out.append(t)
        return out

    def pending(self, account_id: str | None = None
                ) -> list[dict[str, Any]]:
        return self.txns.pending_settlement(account_id)

    def positions(self, account_id: str | None = None
                  ) -> list[dict[str, Any]]:
        """Paper fund units × latest published NAV — mirrors the real
        ``_fund_positions`` valuation contract on the sim journal."""
        seen: set[tuple[str, str, str]] = set()
        for t in self.txns.list(account_id):
            if t["status"] == FundTxnStatus.SETTLED.value:
                seen.add((t["account_id"], t["fund_id"],
                          t["share_class_id"]))
        out: list[dict[str, Any]] = []
        for acct, fid, cls in sorted(seen):
            b = self._basis.basis(acct, fid, cls)
            units = Decimal(str(b.get("units") or 0))
            if units <= 0:
                continue
            latest = self._nav.latest_published(fid, cls)
            nav_row = latest.get("nav") or {}
            nav = Decimal(str(nav_row.get("nav") or 0))
            out.append({
                "account_id": acct, "fund_id": fid,
                "share_class_id": cls,
                "instrument_id": (
                    f"fund:{fid}:{cls}:{nav_row.get('currency') or ''}"),
                "units": str(units),
                "market_value": str(units * nav),
                "currency": nav_row.get("currency") or "TWD",
                "invested_principal": b.get("invested_principal"),
                "unrealized_pnl": b.get("unrealized_pnl"),
                "realized_pnl": b.get("realized_pnl"),
                "data_status": ("estimated" if latest.get("stale")
                                else "ok"),
                "simulated": True,
            })
        return out
