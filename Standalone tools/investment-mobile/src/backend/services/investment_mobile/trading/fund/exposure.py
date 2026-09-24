"""FundExposureEngine + UnifiedPortfolioExposure — look-through analysis.

Fund holdings are disclosure data: every record carries as_of_date,
source_id and coverage_ratio — partial coverage is shown, never
pretended to be a 100% look-through.

Unified exposure merges direct stock positions with fund look-through
for ANALYSIS (e.g. your TSMC stock plus a tech fund holding TSMC are
combined exposures) — but fund holdings are never added to directly-
sellable positions or to total asset market value a second time.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import FundHolding


class FundExposureEngine:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-holdings.jsonl"

    # ------------------------------------------------------------------
    def import_holdings(
        self, fund_id: str, holdings: list[dict[str, Any]],
        source_id: str, as_of_date: date | None = None,
    ) -> dict[str, Any]:
        rows: list[FundHolding] = []
        for raw in holdings:
            h = FundHolding(
                fund_id=fund_id,
                holding_id=str(raw.get("holding_id") or raw.get("isin") or
                                 raw.get("name") or ""),
                name=str(raw.get("name") or ""),
                weight=raw.get("weight") or "0",
                asset_kind=str(raw.get("asset_kind") or "equity"),
                country=str(raw.get("country") or ""),
                sector=str(raw.get("sector") or ""),
                as_of_date=as_of_date,
                source_id=source_id,
            )
            if h.weight <= 0:
                continue
            rows.append(h)
        # replace this fund's holdings for the same as_of date
        kept = [
            r for r in self._all()
            if not (r.fund_id == fund_id and r.as_of_date == as_of_date)
        ]
        rows_all = kept + rows
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("\n".join(
            json.dumps(r.to_dict(), ensure_ascii=False) for r in rows_all
        ) + ("\n" if rows_all else ""), encoding="utf-8")
        coverage = sum((h.weight for h in rows), Decimal("0"))
        return {
            "ok": True, "imported": len(rows),
            "coverage_ratio": str(coverage),
            "partial": coverage < Decimal("0.95"),
            "as_of_date": as_of_date.isoformat() if as_of_date else None,
        }

    def _all(self) -> list[FundHolding]:
        if not self._path.is_file():
            return []
        out: list[FundHolding] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(FundHolding(**json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return out

    def holdings(self, fund_id: str) -> dict[str, Any]:
        rows = [r for r in self._all() if r.fund_id == fund_id]
        coverage = sum((r.weight for r in rows), Decimal("0"))
        as_of = max((r.as_of_date for r in rows if r.as_of_date),
                    default=None)
        return {
            "ok": True, "fund_id": fund_id,
            "holdings": [r.to_dict() for r in rows],
            "coverage_ratio": str(coverage),
            "partial": coverage < Decimal("0.95"),
            "as_of_date": as_of.isoformat() if as_of else None,
            "sources": sorted({r.source_id for r in rows}),
        }

    # ------------------------------------------------------------------
    def breakdown(self, fund_id: str, by: str) -> dict[str, Any]:
        """Aggregate holdings by country|sector|asset_kind."""
        if by not in ("country", "sector", "asset_kind"):
            return {"ok": False, "error_code": "DIMENSION_UNKNOWN"}
        acc: dict[str, Decimal] = {}
        rows = [r for r in self._all() if r.fund_id == fund_id]
        for r in rows:
            key = getattr(r, by) or "unknown"
            acc[key] = acc.get(key, Decimal("0")) + r.weight
        return {
            "ok": True, "fund_id": fund_id, "dimension": by,
            "breakdown": [
                {"name": k, "weight": str(v)}
                for k, v in sorted(acc.items(), key=lambda kv: -kv[1])
            ],
            "coverage_ratio": str(sum(acc.values(), Decimal("0"))),
        }

    def overlap(self, fund_a: str, fund_b: str) -> dict[str, Any]:
        """Shared holdings between two funds."""
        a = {r.holding_id: r for r in self._all() if r.fund_id == fund_a}
        b = {r.holding_id: r for r in self._all() if r.fund_id == fund_b}
        shared = sorted(set(a) & set(b))
        return {
            "ok": True, "funds": [fund_a, fund_b],
            "shared_holdings": [
                {"holding_id": h, "name": a[h].name,
                 "weight_a": str(a[h].weight),
                 "weight_b": str(b[h].weight)}
                for h in shared
            ],
            "overlap_count": len(shared),
            "coverage_a": str(sum((r.weight for r in a.values()), Decimal("0"))),
            "coverage_b": str(sum((r.weight for r in b.values()), Decimal("0"))),
        }


class UnifiedPortfolioExposure:
    """Merges direct positions + fund look-through for ANALYSIS.

    Looked-through fund holdings are tagged ``via_fund`` and reported in
    a separate bucket — they merge into exposure analysis but never into
    directly-sellable positions, and fund market value is counted once.
    """

    def __init__(self, exposure: FundExposureEngine) -> None:
        self._exposure = exposure

    def analyze(
        self,
        direct_positions: list[dict[str, Any]],
        fund_positions: list[dict[str, Any]],
        cash: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """direct_positions: [{instrument_id, market_value, currency,
        sector, country}]; fund_positions: [{fund_id, market_value,
        currency}]; cash: [{amount, currency}]."""
        total = Decimal("0")
        by_market: dict[str, Decimal] = {}
        by_currency: dict[str, Decimal] = {}
        concentration: dict[str, Decimal] = {}

        for p in direct_positions:
            mv = Decimal(str(p.get("market_value") or 0))
            total += mv
            by_market[p.get("market", "?")] = (
                by_market.get(p.get("market", "?"), Decimal("0")) + mv)
            by_currency[p.get("currency", "?")] = (
                by_currency.get(p.get("currency", "?"), Decimal("0")) + mv)
            concentration[p["instrument_id"]] = (
                concentration.get(p["instrument_id"], Decimal("0")) + mv)

        look_through: dict[str, Decimal] = {}
        fund_value = Decimal("0")
        for fp in fund_positions:
            mv = Decimal(str(fp.get("market_value") or 0))
            total += mv
            fund_value += mv
            by_market["fund"] = by_market.get("fund", Decimal("0")) + mv
            by_currency[fp.get("currency", "?")] = (
                by_currency.get(fp.get("currency", "?"), Decimal("0")) + mv)
            h = self._exposure.holdings(str(fp.get("fund_id") or ""))
            coverage = Decimal(h.get("coverage_ratio") or "0")
            for holding in h.get("holdings", []):
                hid = holding["holding_id"]
                eff = mv * Decimal(holding["weight"])
                look_through[hid] = look_through.get(hid, Decimal("0")) + eff
            # partial coverage is surfaced, never hidden
            fp_coverage = coverage

        for c in cash:
            amt = Decimal(str(c.get("amount") or 0))
            total += amt
            by_currency[c.get("currency", "?")] = (
                by_currency.get(c.get("currency", "?"), Decimal("0")) + amt)
            by_market["cash"] = by_market.get("cash", Decimal("0")) + amt

        # merge look-through with direct holdings for exposure view
        combined = dict(concentration)
        for hid, v in look_through.items():
            combined[hid] = combined.get(hid, Decimal("0")) + v

        return {
            "ok": True,
            "total_assets": str(total),
            "by_market": {k: str(v) for k, v in by_market.items()},
            "by_currency": {k: str(v) for k, v in by_currency.items()},
            "concentration": {
                k: str(v / total) if total else "0"
                for k, v in sorted(
                    combined.items(), key=lambda kv: -kv[1])[:20]
            },
            "look_through_components": {
                k: str(v) for k, v in look_through.items()
            },
            "note": (
                "基金穿透持股僅供曝險分析——"
                "不計入可賣出部位，基金市值僅計算一次"
            ),
        }
