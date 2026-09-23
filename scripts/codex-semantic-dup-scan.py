"""§3.5 A — codex semantic duplication scan.

Compares every pair of provisions in the live codex ``articles`` table
(rule + prohibition text, normalized) and classifies duplication:

  exact-identical      — normalized rule AND prohibition text identical
  prohibition-shared   — identical prohibition list, different rule
  template-instance    — high token overlap (>= 0.9 Jaccard) on rule text
  cross-layer          — same subject text on different tiers/sections
  distinct             — below all thresholds (not recorded)

Output: ``governance_rule/execution/audit/convergence/
        codex-semantic-duplication-scan.json``
The report is the evidence that fills the codex
``codex_semantic_duplication_scan`` table (currently a placeholder
``DUPSCAN@2026-09-16`` UNKNOWN row) via a governed amendment.

Evidence-only: never mutates the codex.
"""
from __future__ import annotations

import json
import re
import sys
import time
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = (
    ROOT / "governance_rule" / "execution" / "audit" / "convergence"
    / "codex-semantic-duplication-scan.json"
)

_TEMPLATE_TOKEN_RE = re.compile(r"[A-Z]\d{2,}|['\"\d]")
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")


def _normalize(text: str) -> str:
    """Normalize for exact comparison: case, whitespace, provision ids."""
    t = (text or "").lower()
    t = _TEMPLATE_TOKEN_RE.sub("", t)  # drop ids/numbers so templates match
    return _WS_RE.sub(" ", t).strip()


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _load_provisions(db) -> list[dict]:
    """Unified provision view across the three layers (A/P/E)."""
    tiers = dict(db.execute(
        "SELECT provision_id, tier FROM provision_law_classification"
    ).fetchall())
    provisions: list[dict] = []

    for pid, section, subject, rule, prohibition in db.execute(
        "SELECT provision_id, section_index, subject, rule, prohibition "
        "FROM articles ORDER BY position"
    ):
        provisions.append({
            "id": pid, "type": "article", "section": section,
            "subject": subject or "", "tier": tiers.get(pid, ""),
            "rule_norm": _normalize(rule),
            "prohibition_norm": _normalize(prohibition),
            "rule_tokens": _tokens(f"{subject or ''} {rule or ''}"),
            "subject_norm": _normalize(subject),
        })

    for pid, statement, binding in db.execute(
        "SELECT provision_id, statement, binding FROM principles "
        "ORDER BY position"
    ):
        provisions.append({
            "id": pid, "type": "principle", "section": "principles",
            "subject": "", "tier": tiers.get(pid, ""),
            "rule_norm": _normalize(statement),
            "prohibition_norm": "",
            "rule_tokens": _tokens(statement),
            "subject_norm": "",
        })

    for pid, area, edict, immutability in db.execute(
        "SELECT provision_id, area, edict, immutability FROM edicts "
        "ORDER BY position"
    ):
        provisions.append({
            "id": pid, "type": "edict", "section": "edicts",
            "subject": area or "", "tier": tiers.get(pid, ""),
            "rule_norm": _normalize(edict),
            "prohibition_norm": "",
            "rule_tokens": _tokens(f"{area or ''} {edict or ''}"),
            "subject_norm": _normalize(area),
        })
    return provisions


def main() -> int:
    from governance_rule.execution.codex_repository import (
        codex_readonly_connection,
    )

    with codex_readonly_connection() as db:
        provisions = _load_provisions(db)

    findings: list[dict] = []
    for a, b in combinations(provisions, 2):
        cls = None
        basis = ""
        cross_layer = a["type"] != b["type"]
        if (
            a["rule_norm"] and a["rule_norm"] == b["rule_norm"]
            and a["prohibition_norm"] == b["prohibition_norm"]
        ):
            if cross_layer:
                cls, basis = "cross-layer-restatement", "normalized-text-identical-across-layers"
            else:
                cls, basis = "exact-identical", "normalized-rule+prohibition"
        elif (
            not cross_layer
            and a["prohibition_norm"]
            and a["prohibition_norm"] == b["prohibition_norm"]
            and a["rule_norm"] != b["rule_norm"]
        ):
            cls, basis = "prohibition-shared", "normalized-prohibition"
        else:
            sim = _jaccard(a["rule_tokens"], b["rule_tokens"])
            if cross_layer and sim >= 0.7:
                cls, basis = "cross-layer-restatement", f"token-jaccard={sim:.3f}"
            elif not cross_layer and sim >= 0.9:
                cls, basis = "template-instance", f"rule-jaccard={sim:.3f}"
        if cls:
            findings.append({
                "provision_a": a["id"],
                "provision_b": b["id"],
                "layers": f"{a['type']}<->{b['type']}",
                "classification": cls,
                "similarity_basis": basis,
                "merge_candidate": "review-required",
            })

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["classification"]] = counts.get(f["classification"], 0) + 1

    report = {
        "schema": "codex-semantic-duplication-scan/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provisions_scanned": len(provisions),
        "pairs_examined": len(provisions) * (len(provisions) - 1) // 2,
        "summary": counts,
        "findings": findings,
        "note": (
            "evidence-only scan; fills codex_semantic_duplication_scan "
            "via governed amendment; merge candidates require "
            "manual/sovereign review — never auto-merge"
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "report": str(OUT), "provisions": len(provisions),
        "summary": counts,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
