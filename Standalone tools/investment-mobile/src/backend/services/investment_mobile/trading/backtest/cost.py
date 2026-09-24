"""TradingCostEngine — scoped fee/tax rules, never one hard-coded rate.

Rules keyed on broker / account / market / instrument-kind / direction /
effective dates. Built-in defaults are explicitly tagged assumed=True —
unverified rates are assumptions, not facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class FeeRule:
    rule_id: str
    broker_id: str = ""              # "" = any
    account_id: str = ""
    market: str = ""                 # TAIWAN_EQUITY|US_EQUITY|MUTUAL_FUND
    instrument_kind: str = ""        # stock|etf|fund|""
    direction: str = ""              # buy|sell|subscribe|redeem|""
    kind: str = "percent"            # percent|fixed|min_percent
    rate: Decimal = Decimal("0")
    minimum: Decimal = Decimal("0")
    currency: str = ""
    effective_from: str = ""
    effective_to: str = ""
    assumed: bool = True             # unverified → assumption flag
    source_id: str = "assumption"

    def matches(self, ctx: dict[str, Any], on_date: str) -> bool:
        for key, want in (
            ("broker_id", self.broker_id), ("account_id", self.account_id),
            ("market", self.market),
            ("instrument_kind", self.instrument_kind),
            ("direction", self.direction),
        ):
            if want and str(ctx.get(key) or "") != want:
                return False
        if self.effective_from and on_date < self.effective_from:
            return False
        if self.effective_to and on_date > self.effective_to:
            return False
        return True

    def charge(self, notional: Decimal) -> Decimal:
        if self.kind == "fixed":
            return self.rate
        if self.kind == "min_percent":
            return max(notional * self.rate, self.minimum)
        return notional * self.rate


# Built-in assumed defaults (marked assumed=True — placeholders until
# broker fee schedules are verified).
_DEFAULT_RULES = [
    # 國泰台股：券商手續費 0.1425% 假設折扣率 + 賣出證交稅 0.3%（ETF 0.1%）
    FeeRule("tw-broker-fee", broker_id="CATHAY_SECURITIES",
            market="TAIWAN_EQUITY", instrument_kind="stock",
            kind="percent", rate=Decimal("0.001425"), currency="TWD"),
    FeeRule("tw-sell-tax", market="TAIWAN_EQUITY",
            instrument_kind="stock", direction="sell",
            kind="percent", rate=Decimal("0.003"), currency="TWD"),
    FeeRule("tw-etf-tax", market="TAIWAN_EQUITY",
            instrument_kind="etf", direction="sell",
            kind="percent", rate=Decimal("0.001"), currency="TWD"),
    # 富邦複委託：假設手續費 0.25% 最低 USD 15
    FeeRule("us-subbrokerage", broker_id="FUBON_SUBBROKERAGE",
            market="US_EQUITY", kind="min_percent",
            rate=Decimal("0.0025"), minimum=Decimal("15"),
            currency="USD"),
    # 基金：申購費率依平台/級別（此為假設值）
    FeeRule("fund-subscription", market="MUTUAL_FUND", direction="subscribe",
            kind="percent", rate=Decimal("0.015"), currency="TWD"),
    FeeRule("fund-redemption", market="MUTUAL_FUND", direction="redeem",
            kind="percent", rate=Decimal("0"), currency="TWD"),
]


class TradingCostEngine:
    def __init__(self, state_dir: Path | None = None,
                 extra_rules: list[FeeRule] | None = None) -> None:
        self._path = (Path(state_dir) / "fee_rules.json"
                      if state_dir else None)
        self._rules: list[FeeRule] = list(_DEFAULT_RULES)
        if extra_rules:
            self._rules.extend(extra_rules)
        self._load()

    def add_rule(self, rule: FeeRule) -> dict[str, Any]:
        self._rules.append(rule)
        self._persist()
        return {"ok": True, "rule_id": rule.rule_id,
                "assumed": rule.assumed}

    def rules(self) -> list[dict[str, Any]]:
        return [{
            "rule_id": r.rule_id, "broker_id": r.broker_id,
            "market": r.market, "direction": r.direction,
            "instrument_kind": r.instrument_kind, "kind": r.kind,
            "rate": str(r.rate), "minimum": str(r.minimum),
            "currency": r.currency, "assumed": r.assumed,
            "effective_from": r.effective_from,
            "effective_to": r.effective_to,
        } for r in self._rules]

    def charge(self, *, notional, direction: str, on_date: str,
               ctx: dict[str, Any]) -> dict[str, Any]:
        n = Decimal(str(notional))
        matched = [r for r in self._rules if r.matches(
            {**ctx, "direction": direction}, on_date)]
        total = Decimal("0")
        applied = []
        assumed = False
        for r in matched:
            amt = r.charge(n)
            total += amt
            assumed = assumed or r.assumed
            applied.append({"rule_id": r.rule_id, "amount": str(amt),
                            "assumed": r.assumed})
        return {
            "ok": True, "total": str(total), "applied": applied,
            "assumed": assumed,
            "note": "費率未驗證時標示為假設值" if assumed else "",
        }

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text("utf-8"))
        except ValueError:
            return
        for row in rows:
            self._rules.append(FeeRule(
                rule_id=row["rule_id"], broker_id=row.get("broker_id", ""),
                account_id=row.get("account_id", ""),
                market=row.get("market", ""),
                instrument_kind=row.get("instrument_kind", ""),
                direction=row.get("direction", ""),
                kind=row.get("kind", "percent"),
                rate=Decimal(str(row.get("rate") or 0)),
                minimum=Decimal(str(row.get("minimum") or 0)),
                currency=row.get("currency", ""),
                effective_from=row.get("effective_from", ""),
                effective_to=row.get("effective_to", ""),
                assumed=bool(row.get("assumed", True)),
                source_id=row.get("source_id", "operator")))

    def _persist(self) -> None:
        if not self._path:
            return
        rows = [r for r in self._rules if r not in _DEFAULT_RULES]
        self._path.write_text(json.dumps(
            [{
                "rule_id": r.rule_id, "broker_id": r.broker_id,
                "account_id": r.account_id, "market": r.market,
                "instrument_kind": r.instrument_kind,
                "direction": r.direction, "kind": r.kind,
                "rate": str(r.rate), "minimum": str(r.minimum),
                "currency": r.currency,
                "effective_from": r.effective_from,
                "effective_to": r.effective_to,
                "assumed": r.assumed, "source_id": r.source_id,
            } for r in rows], ensure_ascii=False, indent=1), "utf-8")
