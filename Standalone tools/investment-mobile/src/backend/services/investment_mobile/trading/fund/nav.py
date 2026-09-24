"""FundNAVService — NAV store with published/estimated/confirmed split.

NAV is NOT a realtime tradable price: yesterday's published NAV is never
presented as a guaranteed execution price. When today's NAV is not yet
announced the service keeps the last valid published NAV with its date —
it never fabricates a same-day value. Corrections supersede via higher
``revision`` and are tracked, never silently overwritten.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import FundNAV, NavType
from .providers import import_nav_rows, parse_nav_file


class FundNAVService:
    STALE_AFTER_DAYS = 5            # published NAV older than this → stale flag

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "fund-nav.jsonl"
        self._rev_path = Path(state_dir) / "fund-nav-revisions.jsonl"

    # ------------------------------------------------------------------
    def _read(self) -> list[FundNAV]:
        if not self._path.is_file():
            return []
        out: list[FundNAV] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                out.append(FundNAV(
                    fund_id=row["fund_id"], share_class_id=row["share_class_id"],
                    nav_date=row["nav_date"], nav=row["nav"],
                    currency=row.get("currency", ""),
                    source_id=row.get("source_id", ""),
                    nav_type=row.get("nav_type", NavType.PUBLISHED.value),
                    published_at=row.get("published_at"),
                    received_at=row.get("received_at"),
                    revision=int(row.get("revision") or 1),
                    data_status=row.get("data_status", "ok"),
                ))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return out

    def _rewrite(self, rows: list[FundNAV]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text("\n".join(
            json.dumps(r.to_dict(), ensure_ascii=False) for r in rows
        ) + ("\n" if rows else ""), encoding="utf-8")
        tmp.replace(self._path)

    # ------------------------------------------------------------------
    def record(self, nav: FundNAV) -> dict[str, Any]:
        """Insert or correct a NAV — dedup key + revision supersede."""
        rows = self._read()
        for i, existing in enumerate(rows):
            if existing.dedup_key == nav.dedup_key:
                if nav.revision <= existing.revision:
                    return {"ok": True, "duplicate": True,
                            "nav": existing.to_dict()}
                existing.data_status = "corrected"
                nav.data_status = "ok"
                rows[i] = nav
                self._append(self._rev_path, {
                    "dedup_key": list(nav.dedup_key),
                    "old_revision": existing.revision,
                    "new_revision": nav.revision,
                    "old_nav": str(existing.nav), "new_nav": str(nav.nav),
                    "source_id": nav.source_id,
                })
                self._rewrite(rows)
                return {"ok": True, "corrected": True, "nav": nav.to_dict()}
        rows.append(nav)
        self._rewrite(rows)
        return {"ok": True, "nav": nav.to_dict()}

    def import_file(self, path: Path, source_id: str) -> dict[str, Any]:
        """Controlled file import: parse → validate → dedup → record."""
        raw_rows = parse_nav_file(Path(path))
        valid, rejected = import_nav_rows(raw_rows, source_id)
        inserted = corrected = duplicate = 0
        for nav in valid:
            r = self.record(nav)
            inserted += 1 if not r.get("duplicate") and not r.get("corrected") else 0
            corrected += 1 if r.get("corrected") else 0
            duplicate += 1 if r.get("duplicate") else 0
        return {
            "ok": True, "inserted": inserted, "corrected": corrected,
            "duplicates": duplicate, "rejected": rejected,
            "rejected_count": len(rejected),
            "imported_navs": [n.to_dict() for n in valid],
        }

    # ------------------------------------------------------------------
    def latest(
        self, fund_id: str, share_class_id: str,
        nav_type: str = NavType.PUBLISHED.value,
        as_of: date | None = None,
    ) -> FundNAV | None:
        """Latest NAV of the requested type — types never mix."""
        cands = [
            n for n in self._read()
            if n.fund_id == fund_id and n.share_class_id == share_class_id
            and n.nav_type == nav_type
            and (as_of is None or n.nav_date <= as_of)
        ]
        return max(cands, key=lambda n: n.nav_date) if cands else None

    def latest_published(
        self, fund_id: str, share_class_id: str, as_of: date | None = None,
    ) -> dict[str, Any]:
        """Last officially announced NAV — with staleness flag."""
        nav = self.latest(fund_id, share_class_id,
                          NavType.PUBLISHED.value, as_of)
        if nav is None:
            return {"ok": False, "error_code": "NO_NAV"}
        age = (date.today() - nav.nav_date).days
        return {
            "ok": True, "nav": nav.to_dict(),
            "age_days": age,
            "stale": age > self.STALE_AFTER_DAYS,
            "note": "已公告淨值——非即時成交保證價",
        }

    def history(
        self, fund_id: str, share_class_id: str,
        start: date | None = None, end: date | None = None,
        nav_type: str = NavType.PUBLISHED.value,
    ) -> list[FundNAV]:
        return sorted(
            (n for n in self._read()
             if n.fund_id == fund_id and n.share_class_id == share_class_id
             and n.nav_type == nav_type
             and (start is None or n.nav_date >= start)
             and (end is None or n.nav_date <= end)),
            key=lambda n: n.nav_date,
        )

    def revisions(self) -> list[dict[str, Any]]:
        if not self._rev_path.is_file():
            return []
        return [
            json.loads(l) for l in
            self._rev_path.read_text(encoding="utf-8").splitlines() if l
        ]

    # ------------------------------------------------------------------
    @staticmethod
    def _append(path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
