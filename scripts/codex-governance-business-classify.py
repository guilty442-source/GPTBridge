#!/usr/bin/env python3
"""§3.5-A step 1 — governance vs business rule classification report.

Classifies every effective codex provision as ``governance`` (must stay in
the codex: authority / permission / identity / seal / amendment / audit /
sovereign duties / fail-closed invariants) vs ``business`` (delegatable to
sovereign adjudication: domain behavior, thresholds, budgets, schedules,
SQL lanes, RAG/model parameters, tool-specific policy) vs ``hybrid``
(governance surface + business detail — needs split review).

Inputs (read-only):
- ``main-system/runtime/state/runtime-rule-index.json`` (classifier output:
  per-provision dimensions, subject, tier, section, tokens)
- PostgreSQL codex authority ``postgresql://local/gptbridge_codex``
  (provision text + law classification, governed read-only)

Output: ``governance_rule/execution/audit/convergence/``
``governance-vs-business-rules-<utc>.json``
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INDEX_FILE = ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"
OUT_DIR = ROOT / "governance_rule" / "execution" / "audit" / "convergence"

# The classifier's ``治理/權責`` dimension co-occurs on ~90% of provisions
# (everything in the codex is governance-tagged) — it cannot discriminate.
# ``primary_dimension`` is the discriminative signal.
GOVERNANCE_PRIMARY = {"治理/權責", "安全與稽核"}
BUSINESS_PRIMARY = {"模型/推論", "資料/SQL", "時限/預算"}
AMBIGUOUS_PRIMARY = {"工程實作要求", "流程/程序", "未分類", ""}

# Subjects that are governance machinery even when their primary
# dimension is operational (codex format/permission/identity rules).
GOVERNANCE_SUBJECT_TOKENS = (
    "codex", "permission", "sovereign", "authority", "amendment",
    "seal", "identity", "governance", "ledger", "credential",
    "delegation", "boundary",
)


def classify(prov: dict, rule_text: str) -> tuple[str, str]:
    primary = str(prov.get("primary_dimension") or "")
    subject = str(prov.get("subject") or "").lower()

    if primary in GOVERNANCE_PRIMARY:
        return "governance", f"primary_dimension={primary}"
    if primary in BUSINESS_PRIMARY:
        if any(k in subject for k in GOVERNANCE_SUBJECT_TOKENS):
            return "hybrid", (
                f"primary={primary} but governance subject: {subject}"
            )
        return "business", (
            f"primary={primary}; delegation candidate "
            "(behavior/threshold/budget — sovereign adjudication)"
        )
    return "hybrid", f"primary={primary or 'none'} ambiguous"


def main() -> int:
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    provisions = index.get("provisions") or {}

    # A173: the sqlite predecessor is retired — read the PostgreSQL
    # authority through the governed repository interface.
    from governance_rule.execution.codex_repository import (
        codex_readonly_connection,
    )

    with codex_readonly_connection() as db:
        rules = dict(
            db.execute("SELECT provision_id, rule FROM articles").fetchall()
        )
        law_tier = dict(
            db.execute(
                "SELECT provision_id, tier FROM provision_law_classification"
            ).fetchall()
        )

    rows: list[dict] = []
    counts = {"governance": 0, "business": 0, "hybrid": 0}
    for pid, prov in sorted(provisions.items()):
        cls, rationale = classify(prov, rules.get(pid, ""))
        counts[cls] = counts.get(cls, 0) + 1
        rows.append({
            "provision_id": pid,
            "subject": prov.get("subject"),
            "tier": prov.get("tier"),
            "law_tier": law_tier.get(pid),
            "section": prov.get("section"),
            "lifecycle": prov.get("lifecycle"),
            "primary_dimension": prov.get("primary_dimension"),
            "dimensions": prov.get("dimensions"),
            "prohibition": prov.get("prohibition"),
            "classification": cls,
            "rationale": rationale,
        })

    report = {
        "schema": "governance-vs-business-rules/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": {
            "rule_index": str(INDEX_FILE.relative_to(ROOT)),
            "rule_index_generated": index.get("generated_at_utc"),
            "codex_authority": "postgresql://local/gptbridge_codex",
        },
        "counts": counts,
        "total": len(rows),
        "provisions": rows,
        "notes": [
            "hybrid = both governance and business dimensions — split "
            "review needed (boundary clause stays, behavior delegates)",
            "business = delegation candidates for sovereign adjudication; "
            "codex keeps a reference, not the rule text (§3.5-A step 2)",
            "classification is heuristic (dimensions + tokens + subject); "
            "provision text review required before amendment",
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = OUT_DIR / f"governance-vs-business-rules-{stamp}.json"
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"total={len(rows)} {counts}")
    print(f"report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
