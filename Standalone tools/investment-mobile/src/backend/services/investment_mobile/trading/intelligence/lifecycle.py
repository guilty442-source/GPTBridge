"""RecommendationLifecycle — versioned, append-only recommendation state.

History is never overwritten: every update appends a RecommendationVersion
snapshot. Expiry/invalidation are explicit transitions so users can trace
what was recommended, on which data, and what happened afterwards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    InvestmentRecommendation, RecommendationStatus,
    RecommendationVersion,
)

_ALLOWED: dict[str, frozenset[str]] = {
    RecommendationStatus.CREATED: frozenset({
        RecommendationStatus.VALIDATED, RecommendationStatus.INVALIDATED,
        RecommendationStatus.EXPIRED}),
    RecommendationStatus.VALIDATED: frozenset({
        RecommendationStatus.PUBLISHED, RecommendationStatus.INVALIDATED,
        RecommendationStatus.EXPIRED}),
    RecommendationStatus.PUBLISHED: frozenset({
        RecommendationStatus.ACKNOWLEDGED, RecommendationStatus.EXPIRED,
        RecommendationStatus.INVALIDATED}),
    RecommendationStatus.ACKNOWLEDGED: frozenset({
        RecommendationStatus.EXPIRED, RecommendationStatus.INVALIDATED,
        RecommendationStatus.ARCHIVED}),
    RecommendationStatus.EXPIRED: frozenset({RecommendationStatus.ARCHIVED}),
    RecommendationStatus.INVALIDATED: frozenset({RecommendationStatus.ARCHIVED}),
    RecommendationStatus.ARCHIVED: frozenset(),
}


class RecommendationLifecycle:
    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._recs_path = self._dir / "recommendations.jsonl"
        self._ver_path = self._dir / "recommendation_versions.jsonl"
        self._cache: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    def create(self, rec: InvestmentRecommendation) -> dict[str, Any]:
        errors = rec.validate()
        if errors:
            return {"ok": False, "errors": errors}
        self._load()
        self._cache[rec.recommendation_id] = rec.to_dict()
        self._append(self._recs_path, rec.to_dict())
        self._snapshot(rec.recommendation_id, "created", rec.to_dict())
        return {"ok": True, "recommendation": rec.to_dict()}

    def transition(self, rec_id: str, target: str, *,
                   reason: str = "", actor: str = "") -> dict[str, Any]:
        self._load()
        cur = self._cache.get(rec_id)
        if cur is None:
            return {"ok": False, "error_code": "REC_NOT_FOUND"}
        if target not in RecommendationStatus.ALL:
            return {"ok": False, "error_code": "STATUS_UNKNOWN"}
        if target not in _ALLOWED.get(cur["status"], frozenset()):
            return {"ok": False, "error_code": "ILLEGAL_TRANSITION",
                    "from": cur["status"], "to": target}
        self._snapshot(rec_id, reason or f"→{target}", cur, actor)
        cur["status"] = target
        self._rewrite()
        return {"ok": True, "recommendation_id": rec_id, "status": target}

    def expire_due(self, now: datetime | None = None) -> dict[str, Any]:
        """Auto-expire published/acknowledged recs past expires_at."""
        now = now or datetime.now(timezone.utc)
        expired = []
        self._load()
        for rec_id, rec in self._cache.items():
            if rec["status"] in (RecommendationStatus.PUBLISHED,
                                 RecommendationStatus.ACKNOWLEDGED,
                                 RecommendationStatus.VALIDATED,
                                 RecommendationStatus.CREATED):
                exp = rec.get("expires_at")
                if exp:
                    try:
                        if datetime.fromisoformat(exp) <= now:
                            self.transition(
                                rec_id, RecommendationStatus.EXPIRED,
                                reason="expires_at reached", actor="system")
                            expired.append(rec_id)
                    except ValueError:
                        continue
        return {"ok": True, "expired": expired}

    # ------------------------------------------------------------------
    def get(self, rec_id: str) -> dict[str, Any] | None:
        self._load()
        return self._cache.get(rec_id)

    def list(self, status: str | None = None,
             instrument_id: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        self._load()
        rows = list(self._cache.values())
        if status:
            rows = [r for r in rows if r["status"] == status]
        if instrument_id:
            rows = [r for r in rows if r.get("instrument_id") == instrument_id]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[: int(limit)]

    def versions(self, rec_id: str) -> list[dict[str, Any]]:
        if not self._ver_path.exists():
            return []
        out = []
        for line in self._ver_path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("recommendation_id") == rec_id:
                out.append(row)
        return out

    # ------------------------------------------------------------------
    def _snapshot(self, rec_id: str, reason: str,
                  snapshot: dict[str, Any], actor: str = "") -> None:
        versions = self.versions(rec_id)
        ver = RecommendationVersion(
            recommendation_id=rec_id, version=len(versions) + 1,
            snapshot=dict(snapshot), change_reason=reason,
            changed_by=actor or "system")
        self._append(self._ver_path, ver.to_dict())

    def _load(self) -> None:
        if self._cache is not None:
            return
        self._cache = {}
        if self._recs_path.exists():
            for line in self._recs_path.read_text("utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rid = row.get("recommendation_id")
                if rid:
                    self._cache[rid] = row

    def _rewrite(self) -> None:
        with self._recs_path.open("w", encoding="utf-8") as fh:
            for row in self._cache.values():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _append(path: Path, row: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
