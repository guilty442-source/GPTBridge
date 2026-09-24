"""FundDistributionService — distributions with source attribution.

Distributions are classified income / principal (return of capital) /
unconfirmed. Total-return-with-distribution is computed explicitly —
a high distribution rate is never presented as investment return, and
a distribution is never double-counted as both asset value and income.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import DistributionSource, FundDistribution, FundNAV


class FundDistributionService:
    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-distributions.jsonl"

    def record(self, dist: FundDistribution) -> dict[str, Any]:
        for existing in self.list(dist.share_class_id):
            if (existing["distribution_id"] == dist.distribution_id
                    or (existing["ex_distribution_date"]
                        == dist.ex_distribution_date.isoformat()
                        and existing["amount_per_unit"]
                        == str(dist.amount_per_unit))):
                return {"ok": True, "duplicate": True,
                        "distribution": existing}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dist.to_dict(), ensure_ascii=False) + "\n")
        return {"ok": True, "distribution": dist.to_dict()}

    def list(
        self, share_class_id: str | None = None,
        fund_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if share_class_id and row.get("share_class_id") != share_class_id:
                continue
            if fund_id and row.get("fund_id") != fund_id:
                continue
            out.append(row)
        return sorted(out, key=lambda r: r["ex_distribution_date"])

    # ------------------------------------------------------------------
    def totals(
        self, share_class_id: str,
        start: date | None = None, end: date | None = None,
    ) -> dict[str, Any]:
        """Cumulative distributions split by source — the
        principal-vs-income distinction is always visible."""
        income = principal = unconfirmed = Decimal("0")
        count = 0
        for d in self.list(share_class_id):
            ex = date.fromisoformat(d["ex_distribution_date"])
            if start and ex < start or end and ex > end:
                continue
            amt = Decimal(d["amount_per_unit"])
            src = d.get("distribution_source") or DistributionSource.UNCONFIRMED.value
            if src == DistributionSource.INCOME.value:
                income += amt
            elif src == DistributionSource.PRINCIPAL.value:
                principal += amt
            else:
                unconfirmed += amt
            count += 1
        return {
            "share_class_id": share_class_id,
            "count": count,
            "income_per_unit": str(income),
            "principal_per_unit": str(principal),
            "unconfirmed_per_unit": str(unconfirmed),
            "total_per_unit": str(income + principal + unconfirmed),
        }

    def total_return_navs(
        self, navs: list[FundNAV], share_class_id: str,
    ) -> list[dict[str, Any]]:
        """NAV series adjusted for distributions (reinvestment basis).

        Returns (date, adjusted_nav) — cumulative per-unit distributions
        are added back so returns include payouts. NAV itself is never
        rewritten; this is a derived series.
        """
        dists = self.list(share_class_id)
        out: list[dict[str, Any]] = []
        for nav in navs:
            paid = sum(
                Decimal(d["amount_per_unit"]) for d in dists
                if date.fromisoformat(d["ex_distribution_date"]) <= nav.nav_date
            )
            out.append({
                "nav_date": nav.nav_date.isoformat(),
                "nav": str(nav.nav),
                "cum_distribution_per_unit": str(paid),
                "total_return_value": str(nav.nav + paid),
            })
        return out
