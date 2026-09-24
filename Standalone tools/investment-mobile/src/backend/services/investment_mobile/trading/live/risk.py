"""LiveRiskEngine — deterministic, LLM-independent risk gate.

Composes four controllers behind one verdict:

- ``CapitalRiskController``  — funds caps + reserve/release (a new order
  can never consume cash another order already reserved).
- ``PositionRiskController`` — instrument/ETF/market/sector/strategy/
  concentration caps incl. related-asset exposure groups (TSM 2330 ↔
  TSM ADR ↔ tech ETFs flagged as related, never merged into one
  tradable position).
- ``LossRiskController``     — daily/weekly/monthly loss, drawdown,
  per-strategy loss; breach latches a persisted circuit breaker that
  only an authorized human resume can release.
- ``CrossMarketRiskService`` — portfolio-wide aggregation across
  accounts/currencies; accounts stay fund-isolated.

Verdicts: ALLOW / DENY / INCOMPLETE_EVIDENCE. Missing required inputs
never produce ALLOW. Limits come from ``live-risk-limits.json`` —
proposal-supplied params are informational only.
"""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import LiveRiskDecision, RiskVerdict

_D = Decimal

_DEFAULT_LIMITS: dict[str, Any] = {
    # capital
    "max_order_notional": "0",            # 0 = unset → INCOMPLETE
    "max_daily_notional": "0",
    "max_strategy_capital": "0",
    "max_account_daily_notional": "0",
    "max_total_risk_fraction": "0",       # of total assets
    "max_usable_capital_fraction": "0",   # of available cash
    # position
    "max_position_notional": "0",
    "max_etf_position_notional": "0",
    "max_market_fraction": "0",
    "max_sector_fraction": "0",
    "max_strategy_position_fraction": "0",
    "max_concentration_fraction": "0",
    "max_related_exposure_fraction": "0",
    # loss / drawdown
    "max_trade_risk_budget": "0",
    "max_daily_loss": "0",
    "max_weekly_loss": "0",
    "max_monthly_loss": "0",
    "max_drawdown_fraction": "0",
    "max_strategy_loss": "0",
    # evidence
    "max_quote_age_s": 30.0,
    "require_session_open": True,
}

_RESUME_ISSUERS = frozenset({"governor", "user", "risk-officer"})
_AI_ACTORS = frozenset({
    "ai", "xingcheng", "model", "llm", "assistant", "agent",
    "local-model", "星澄"})


# ======================================================================
# Capital
# ======================================================================

class CapitalRiskController:
    """Funds caps + reservation registry (no double-spend of cash)."""

    def __init__(self) -> None:
        self._reservations: dict[str, _D] = {}   # ref → amount

    def reserve(self, ref: str, amount) -> dict[str, Any]:
        if ref in self._reservations:
            return {"ok": False, "error_code": "RESERVATION_EXISTS"}
        self._reservations[ref] = _D(str(amount))
        return {"ok": True}

    def release(self, ref: str) -> _D:
        return self._reservations.pop(ref, _D("0"))

    def reserved_total(self) -> _D:
        return sum(self._reservations.values(), _D("0"))

    def check(self, ctx: dict[str, Any], limits: dict[str, Any]
              ) -> list[str]:
        r: list[str] = []
        notional = _D(str(ctx.get("notional") or 0))
        available = _D(str(ctx.get("cash_available") or 0))
        daily_traded = _D(str(ctx.get("daily_traded_notional") or 0))
        strategy_used = _D(str(ctx.get("strategy_capital_used") or 0))
        total_assets = _D(str(ctx.get("total_assets") or 0))
        is_buy = str(ctx.get("side")) in ("buy", "subscribe")

        cap = _D(str(limits["max_order_notional"]))
        if cap <= 0:
            r.append("capital_limit_unset")
        elif notional > cap:
            r.append("order_notional_exceeds_limit")

        cap = _D(str(limits["max_daily_notional"]))
        if cap > 0 and daily_traded + notional > cap:
            r.append("daily_capital_exceeds_limit")

        cap = _D(str(limits["max_strategy_capital"]))
        if cap > 0 and strategy_used + notional > cap:
            r.append("strategy_capital_exceeds_limit")

        cap = _D(str(limits["max_account_daily_notional"]))
        if cap > 0 and daily_traded + notional > cap:
            r.append("account_daily_capital_exceeds_limit")

        frac = _D(str(limits["max_total_risk_fraction"]))
        if frac > 0 and total_assets > 0:
            if notional / total_assets > frac:
                r.append("total_asset_risk_exceeds_limit")

        if is_buy:
            free = available - self.reserved_total()
            frac = _D(str(limits["max_usable_capital_fraction"]))
            usable = available * frac if frac > 0 else available
            if free <= 0:
                r.append("no_unreserved_cash")
            elif notional > free:
                r.append("cash_reserved_by_other_orders")
            elif frac > 0 and notional > usable:
                r.append("usable_capital_fraction_exceeded")
        return r


# ======================================================================
# Position
# ======================================================================

class PositionRiskController:
    """Position/concentration caps incl. related-exposure groups."""

    def __init__(self) -> None:
        # instrument_id → related-exposure group id (TSM group etc.)
        self._related_groups: dict[str, str] = {}
        # instrument_id → sector id
        self._sectors: dict[str, str] = {}

    def register_related(self, group_id: str,
                         instrument_ids: list[str]) -> None:
        for iid in instrument_ids:
            self._related_groups[str(iid)] = str(group_id)

    def register_sector(self, instrument_id: str, sector: str) -> None:
        self._sectors[str(instrument_id)] = str(sector)

    def check(self, ctx: dict[str, Any], limits: dict[str, Any]
              ) -> list[str]:
        r: list[str] = []
        notional = _D(str(ctx.get("notional") or 0))
        side = str(ctx.get("side"))
        is_buy = side in ("buy", "subscribe")
        positions = ctx.get("positions") or []
        equity = _D(str(ctx.get("total_assets") or 0))
        iid = str(ctx.get("instrument_id") or "")
        market = str(ctx.get("market") or "")
        strategy_id = str(ctx.get("strategy_id") or "")
        kind = str(ctx.get("instrument_kind") or "")

        cur = next((p for p in positions
                    if str(p.get("instrument_id")) == iid), None)
        cur_notional = _D(str((cur or {}).get("notional") or 0))
        cur_value = _D(str((cur or {}).get("market_value") or 0))
        delta = notional if is_buy else -notional
        projected = cur_notional + delta

        cap = _D(str(limits["max_position_notional"]))
        if cap > 0 and projected > cap:
            r.append("position_notional_exceeds_limit")

        if kind.upper().endswith("ETF"):
            cap = _D(str(limits["max_etf_position_notional"]))
            if cap > 0 and projected > cap:
                r.append("etf_position_exceeds_limit")

        if equity > 0:
            frac = _D(str(limits["max_concentration_fraction"]))
            if frac > 0 and (cur_value + delta) / equity > frac:
                r.append("concentration_exceeds_limit")

            market_value = sum(
                _D(str(p.get("market_value") or 0)) for p in positions
                if str(p.get("market") or "") == market)
            frac = _D(str(limits["max_market_fraction"]))
            if frac > 0 and (market_value + delta) / equity > frac:
                r.append("market_fraction_exceeds_limit")

            sector = self._sectors.get(iid)
            if sector:
                sector_value = sum(
                    _D(str(p.get("market_value") or 0))
                    for p in positions
                    if self._sectors.get(
                        str(p.get("instrument_id"))) == sector)
                frac = _D(str(limits["max_sector_fraction"]))
                if frac > 0 and (sector_value + delta) / equity > frac:
                    r.append("sector_fraction_exceeds_limit")

            if strategy_id:
                strat_value = sum(
                    _D(str(p.get("market_value") or 0))
                    for p in positions
                    if str(p.get("strategy_id") or "") == strategy_id)
                frac = _D(str(limits["max_strategy_position_fraction"]))
                if frac > 0 and (strat_value + delta) / equity > frac:
                    r.append("strategy_position_exceeds_limit")

            group = self._related_groups.get(iid)
            if group:
                related = sum(
                    _D(str(p.get("market_value") or 0))
                    for p in positions
                    if self._related_groups.get(
                        str(p.get("instrument_id"))) == group)
                frac = _D(str(limits["max_related_exposure_fraction"]))
                if frac > 0 and (related + delta) / equity > frac:
                    r.append("related_exposure_exceeds_limit")
        if not is_buy and projected < 0:
            r.append("sell_exceeds_position")
        return r


# ======================================================================
# Loss / drawdown
# ======================================================================

class LossRiskController:
    """Loss limits + persisted circuit breakers.

    A breach latches a breaker that blocks NEW risk; reducing existing
    risk stays possible but still requires authorization + risk pass.
    Breaker release is an explicit audited act by an authorized actor —
    never by AI.
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "live_circuit_breakers.json"
        self._breakers: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._breakers = data
        except Exception:
            self._breakers = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._breakers, ensure_ascii=False,
                                  indent=1), "utf-8")
        tmp.replace(self._path)

    def breakers(self) -> dict[str, dict[str, Any]]:
        return dict(self._breakers)

    def resume(self, scope: str, by: str, evidence: str = ""
               ) -> dict[str, Any]:
        """Authorized release — AI actors structurally refused."""
        if str(by).lower() in _AI_ACTORS:
            return {"ok": False, "error_code": "AI_CANNOT_RELEASE"}
        if str(by) not in _RESUME_ISSUERS:
            return {"ok": False, "error_code": "RESUME_NOT_AUTHORIZED"}
        br = self._breakers.pop(scope, None)
        if br is None:
            return {"ok": False, "error_code": "NO_ACTIVE_BREAKER"}
        self._persist()
        return {"ok": True, "released": scope, "by": by,
                "evidence": evidence}

    def check(self, ctx: dict[str, Any], limits: dict[str, Any]
              ) -> tuple[list[str], list[str]]:
        """Return (reasons, breakers_triggered)."""
        r: list[str] = []
        fired: list[str] = []
        acct = str(ctx.get("account_id") or "")
        if any(b.get("account_id") in ("", acct)
               for b in self._breakers.values()):
            r.append("circuit_breaker_active")
        pnl = ctx.get("pnl") or {}
        checks = (
            ("daily", "max_daily_loss", "daily_loss_limit_breached"),
            ("weekly", "max_weekly_loss", "weekly_loss_limit_breached"),
            ("monthly", "max_monthly_loss", "monthly_loss_limit_breached"),
        )
        for key, lim, code in checks:
            cap = _D(str(limits[lim]))
            if cap > 0 and _D(str(pnl.get(key) or 0)) <= -cap:
                r.append(code)
                fired.append(f"{key}:{acct}")
        dd = _D(str(limits["max_drawdown_fraction"]))
        peak = _D(str(ctx.get("peak_equity") or 0))
        eq = _D(str(ctx.get("total_assets") or 0))
        if dd > 0 and peak > 0 and (peak - eq) / peak >= dd:
            r.append("max_drawdown_breached")
            fired.append(f"drawdown:{acct}")
        cap = _D(str(limits["max_strategy_loss"]))
        sloss = _D(str(ctx.get("strategy_realized_loss") or 0))
        if cap > 0 and sloss <= -cap:
            r.append("strategy_loss_limit_breached")
            fired.append(f"strategy:{ctx.get('strategy_id')}")
        budget = _D(str(limits["max_trade_risk_budget"]))
        risk_amt = _D(str(ctx.get("trade_risk_amount") or 0))
        if budget > 0 and risk_amt > budget:
            r.append("trade_risk_budget_exceeded")
        for scope in fired:
            if scope not in self._breakers:
                self._breakers[scope] = {
                    "scope": scope, "account_id": acct,
                    "reason": ",".join(r), "at": time.time()}
        if fired:
            self._persist()
        return r, fired


# ======================================================================
# Cross-market aggregation
# ======================================================================

class CrossMarketRiskService:
    """Portfolio-wide view across accounts/currencies — advisory flags.

    Accounts stay fund-isolated; this service only measures aggregate
    exposure (with explicit FX rates) and never assumes instant cash
    transfer between brokers.
    """

    def __init__(self, fx: Any | None = None) -> None:
        self._fx = fx   # CurrencyRateService — convert(amount, fr, to)

    def aggregate(self, accounts_state: list[dict[str, Any]],
                  base_currency: str = "TWD") -> dict[str, Any]:
        total = _D("0")
        by_currency: dict[str, _D] = {}
        by_market: dict[str, _D] = {}
        warnings: list[str] = []
        for acct in accounts_state:
            ccy = str(acct.get("currency") or base_currency)
            assets = _D(str(acct.get("total_assets") or 0))
            if ccy != base_currency:
                rate = acct.get("fx_rate")
                if rate is None:
                    warnings.append(f"fx_missing:{ccy}")
                    continue
                assets = assets * _D(str(rate))
            total += assets
            by_currency[ccy] = by_currency.get(ccy, _D("0")) + assets
            mkt = str(acct.get("market") or "")
            by_market[mkt] = by_market.get(mkt, _D("0")) + assets
        return {
            "total_assets_base": str(total),
            "base_currency": base_currency,
            "by_currency": {k: str(v) for k, v in by_currency.items()},
            "by_market": {k: str(v) for k, v in by_market.items()},
            "warnings": warnings,
        }


# ======================================================================
# Engine
# ======================================================================

_REQUIRED_CTX = (
    "account_id", "instrument_id", "side", "quantity",
    "notional", "cash_available", "total_assets", "market",
)

_INCOMPLETE = {
    "cash_available": "cash_unknown",
    "total_assets": "equity_unknown",
    "market": "market_unknown",
    "notional": "price_unknown",
}


class LiveRiskEngine:
    """Deterministic verdict composer — no LLM anywhere in this file."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._limits_path = self._dir / "live-risk-limits.json"
        self._limits = self._load_limits()
        self.capital = CapitalRiskController()
        self.position = PositionRiskController()
        self.loss = LossRiskController(self._dir)
        self.cross_market = CrossMarketRiskService()

    # ------------------------------------------------------------------
    def _load_limits(self) -> dict[str, Any]:
        limits = dict(_DEFAULT_LIMITS)
        try:
            user = json.loads(
                self._limits_path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                limits.update(user)
        except Exception:
            pass
        return limits

    @property
    def limits(self) -> dict[str, Any]:
        return dict(self._limits)

    # ------------------------------------------------------------------
    def evaluate(self, ctx: dict[str, Any]) -> LiveRiskDecision:
        decision = LiveRiskDecision(
            verdict=RiskVerdict.DENY.value,
            proposal_id=str(ctx.get("proposal_id") or ""),
            account_id=str(ctx.get("account_id") or ""),
            strategy_id=str(ctx.get("strategy_id") or ""),
        )
        incomplete: list[str] = []
        reasons: list[str] = []

        # evidence completeness first — missing data never allows
        for key in _REQUIRED_CTX:
            if ctx.get(key) in (None, ""):
                incomplete.append(_INCOMPLETE.get(key, f"{key}_missing"))
        qty = _D(str(ctx.get("quantity") or 0))
        if qty <= 0:
            reasons.append("quantity_invalid")
        age = ctx.get("quote_age_s")
        if age is None:
            incomplete.append("market_data_age_unknown")
        elif float(age) > float(self._limits["max_quote_age_s"]):
            reasons.append("stale_market_data")
        if self._limits.get("require_session_open", True):
            if ctx.get("session_open") is None:
                incomplete.append("session_state_unknown")
            elif ctx.get("session_open") is False:
                reasons.append("market_closed")

        reasons += self.capital.check(ctx, self._limits)
        reasons += self.position.check(ctx, self._limits)
        loss_reasons, _ = self.loss.check(ctx, self._limits)
        reasons += loss_reasons

        if incomplete:
            decision.verdict = RiskVerdict.INCOMPLETE_EVIDENCE.value
            decision.reason_codes = incomplete + reasons
        elif reasons:
            decision.verdict = RiskVerdict.DENY.value
            decision.reason_codes = reasons
        else:
            decision.verdict = RiskVerdict.ALLOW.value
        return decision
