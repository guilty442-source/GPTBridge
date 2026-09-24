"""AnalysisEvidenceService — provenance gate for every analysis output.

Rules enforced here:
- VERIFIED_FACT requires a source_id.
- CALCULATED_RESULT requires a named deterministic computation.
- Source-less numeric claims are refused into decision-grade evidence.
- Missing data is recorded, never silently back-filled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import AnalysisEvidence, EvidenceKind


class EvidenceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AnalysisEvidenceService:
    """Journal of evidence attached to analysis runs + validation gate."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "intelligence_evidence.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def attest(self, ev: AnalysisEvidence) -> dict[str, Any]:
        """Validate then journal. Raises EvidenceError on refusal."""
        errors = ev.validate()
        if errors:
            return {"ok": False, "errors": errors}
        self._append(ev.to_dict())
        return {"ok": True, "evidence_id": ev.evidence_id}

    def attest_many(
        self, items: list[AnalysisEvidence]
    ) -> dict[str, Any]:
        accepted: list[str] = []
        rejected: list[dict[str, Any]] = []
        for ev in items:
            res = self.attest(ev)
            if res.get("ok"):
                accepted.append(ev.evidence_id)
            else:
                rejected.append(
                    {"claim": ev.claim[:80], "errors": res["errors"]})
        return {"ok": not rejected, "accepted": accepted,
                "rejected": rejected}

    # ------------------------------------------------------------------
    def grade_data_quality(
        self, evidence: list[AnalysisEvidence],
        required_kinds: tuple[str, ...] = (EvidenceKind.VERIFIED_FACT,
                                         EvidenceKind.CALCULATED_RESULT),
    ) -> str:
        """complete / partial / degraded based on evidence coverage."""
        kinds = {e.kind for e in evidence}
        if all(k in kinds for k in required_kinds):
            return "complete"
        if EvidenceKind.CALCULATED_RESULT in kinds:
            return "partial"
        return "degraded"

    def missing_markers(
        self, evidence: list[AnalysisEvidence],
    ) -> list[str]:
        return [
            e.claim for e in evidence
            if e.kind == EvidenceKind.UNVERIFIED_INFORMATION
        ]

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("evidence_id") in row.get("run_ids", []):
                out.append(row)
        return out

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        lines = self._path.read_text("utf-8").splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]

    # ------------------------------------------------------------------
    def _append(self, row: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
