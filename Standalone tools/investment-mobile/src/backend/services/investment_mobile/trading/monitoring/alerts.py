"""InvestmentAlertEngine — user/strategy-owned rules, deterministic
severity.

AlertSeverity: INFO < NOTICE < WARNING < CRITICAL — decided by RULES
(threshold crossings), never by LLM judgement alone.

Rules come from the user or an authorized strategy version — 星澄 cannot
mutate formal risk limits; attempts are denied explicitly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

RULE_KINDS = frozenset({
    "price_above", "price_below", "ma_cross", "volume_spike",
    "volatility_above", "holding_loss_below", "allocation_drift",
    "fund_nav_change", "fund_announcement",
})
SEVERITY_ORDER = {"INFO": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}


class InvestmentAlertEngine:
    def __init__(self, state_dir: Path, events: Any) -> None:
        self._path = state_dir / "monitoring" / "alert-rules.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._events = events
        self._rules: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            self._rules = json.loads(
                self._path.read_text(encoding="utf-8"))
        except Exception:
            self._rules = []

    def _persist(self) -> None:
        self._path.write_text(json.dumps(
            self._rules, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    def set_rule(self, rule: dict[str, Any], *,
                 actor: str = "user") -> dict[str, Any]:
        if actor.lower() in ("ai", "xingcheng", "model", "assistant"):
            return {"ok": False, "error_code": "AI_CANNOT_SET_RULE"}
        kind = str(rule.get("kind") or "")
        if kind not in RULE_KINDS:
            return {"ok": False, "error_code": "RULE_KIND_UNKNOWN",
                    "kinds": sorted(RULE_KINDS)}
        rule = dict(rule)
        rule.setdefault("rule_id",
                        f"alr-{abs(hash(json.dumps(rule, sort_keys=True))) & 0xffffffff:x}")
        rule["actor"] = actor
        rule["enabled"] = bool(rule.get("enabled", True))
        rule["severity"] = rule.get("severity", "WARNING")
        if rule["severity"] not in SEVERITY_ORDER:
            return {"ok": False, "error_code": "SEVERITY_UNKNOWN"}
        self._rules = [r for r in self._rules
                       if r["rule_id"] != rule["rule_id"]]
        self._rules.append(rule)
        self._persist()
        return {"ok": True, "rule_id": rule["rule_id"]}

    def list_rules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rules]

    # ------------------------------------------------------------------
    def evaluate(self, *, market_data: dict[str, Any] | None = None,
                 valuation: dict[str, Any] | None = None,
                 allocation: dict[str, Any] | None = None,
                 indicators: dict[str, Any] | None = None
                 ) -> dict[str, Any]:
        """Fire rules against deterministic inputs → MonitoringEvents."""
        fired = []
        market_data = market_data or {}
        indicators = indicators or {}
        for rule in self._rules:
            if not rule.get("enabled"):
                continue
            hit, sev, detail = self._check(rule, market_data,
                                           valuation, allocation,
                                           indicators)
            if hit:
                r = self._events.emit(
                    "risk_threshold" if sev == "CRITICAL"
                    else "indicator_signal",
                    instrument_id=str(rule.get("instrument_id") or ""),
                    account_id=str(rule.get("account_id") or ""),
                    market=str(rule.get("market") or ""),
                    severity=sev,
                    state_token=str(rule["rule_id"]),
                    detail={"rule_id": rule["rule_id"], **detail})
                if r.get("ok"):
                    fired.append(r["event"])
        return {"ok": True, "fired": fired, "rules": len(self._rules)}

    def _check(self, rule: dict[str, Any], md: dict[str, Any],
               val: dict[str, Any] | None, alloc: dict[str, Any] | None,
               ind: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        kind = rule["kind"]
        thr = float(rule.get("threshold") or 0)
        iid = str(rule.get("instrument_id") or "")
        if kind == "price_above":
            p = md.get(iid, {}).get("close")
            return (p is not None and float(p) >= thr,
                    rule["severity"], {"price": p, "threshold": thr})
        if kind == "price_below":
            p = md.get(iid, {}).get("close")
            return (p is not None and float(p) <= thr,
                    rule["severity"], {"price": p, "threshold": thr})
        if kind == "volatility_above":
            v = (ind.get(iid) or {}).get("volatility_annual")
            return (v is not None and float(v) >= thr,
                    rule["severity"], {"volatility": v})
        if kind == "holding_loss_below" and val is not None:
            for p in val.get("positions", []):
                if iid and p["instrument_id"] != iid:
                    continue
                if p.get("unrealized_pnl"):
                    try:
                        cost = float(p["average_cost"]) * float(
                            p["quantity"])
                        pnl = float(p["unrealized_pnl"]) / cost \
                            if cost else 0
                        if pnl <= thr:
                            return True, rule["severity"], {
                                "instrument_id": p["instrument_id"],
                                "pnl_pct": pnl, "threshold": thr}
                    except Exception:
                        continue
            return False, rule["severity"], {}
        if kind == "allocation_drift" and alloc is not None:
            for b in alloc.get("breaches", []):
                if abs(float(b["drift"])) >= thr:
                    return True, rule["severity"], dict(b)
            return False, rule["severity"], {}
        return False, rule["severity"], {}
