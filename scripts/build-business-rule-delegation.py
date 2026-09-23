#!/usr/bin/env python3
"""§3.5 E — 法典業務規則下放對照清單（治理 vs 業務）

以 ``main-system/runtime/state/runtime-rule-index.json`` 的
``primary_dimension`` 分類為底，產出一份「條文→治理／業務」對照清單，
並標示 owning 主宰／模組（以 index 的 owners / module_assignment_registry 對照）。

 governance（法典保留）： 治理/權責、安全與稽核、時限/預算（治理側）
 business （下放）：  模型/推論、資料/SQL、工程實作要求、流程/程序、未分類

輸出： ``governance_rule/execution/audit/convergence/business_rule_delegation.json``
      與同名 .csv（供治理審閱）。兩份皆為派生、可重建、可刪除，不改法典效力。

用法：
  python scripts/build-business-rule-delegation.py
  python scripts/build-business-rule-delegation.py --summary
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"
OUT_JSON = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "convergence" / "business_rule_delegation.json"
OUT_CSV = OUT_JSON.with_suffix(".csv")

GOVERNANCE_DIMS = {"治理/權責", "安全與稽核", "時限/預算"}
# 其餘視為 business（模型、資料、工程、流程）
SCHEMA = "business-rule-delegation/v2"

# §10.68 單元制：section → 業務主宰單元（E② 歸屬標示）。
# 未列出的 section 一律 unassigned-review-needed（fail-closed：不猜測歸屬）。
SECTION_TO_UNIT = {
    "system-responsibility": "UNIT_RUNTIME",
    "星澄模型與主系統整合條例": "UNIT_ENGINE",
    "learning-system-special-law": "UNIT_ENGINE",
    "model-resource-gpu-special-law": "UNIT_ENGINE",
    "data-authority-storage-rag-special-law": "UNIT_DATA",
    "startup-control-special-law": "UNIT_BOOTSTRAP",
    "runtime-lifecycle-fault-isolation-special-law": "UNIT_RUNTIME",
    "version-release-hot-update-rollback-special-law": "UNIT_AUTOMATION",
    "git-worktree-special-law": "UNIT_AUTOMATION",
    "automatic-maintenance-repair-special-law": "UNIT_AUTOMATION",
    "disaster-recovery-continuity-special-law": "UNIT_AUTOMATION",
    "test-validation-evaluation-special-law": "UNIT_PERMISSION",
    "independent-tool-lifecycle-isolation-special-law": "BLOCK_TOOLS",
    "ui-window-projection-synchronization-special-law": "BLOCK_TOOLS",
    "language-source-native-boundary-special-law": "UNIT_PLATFORM",
    "information-communication-special-law": "UNIT_PLATFORM",
    "top-level-directory-path-special-law": "UNIT_PERMISSION",
    "directory-special-law": "UNIT_PERMISSION",
    "permission": "UNIT_PERMISSION",
    "permission-identity-special-law": "UNIT_PERMISSION",
    "identity-group-membership-and-authority-special-law": "UNIT_PERMISSION",
    "星澄-chinese-codex-confidentiality-special-law": "UNIT_ASSISTANT",
    "privacy-personal-data-special-law": "UNIT_DATA",
    "cryptography-key-management-special-law": "UNIT_PERMISSION",
    "audit-evidence-timestamp-retention-special-law": "UNIT_PERMISSION",
    "third-party-network-supply-chain-special-law": "UNIT_PERMISSION",
    "mandatory-implementation-obligation-special-law": "UNIT_DECISION",
    "system-reliability-performance-repair-isolation-special-law": "UNIT_RUNTIME",
}

# §3.5 E① 裁定：憲制性 meta 章節（總則／主權／修訂保護／分權問責／解釋衝突／
# 閉鎖安全／反越獄）即使含業務維度，其內容描述的是治理自身運作而非業務單元
# 職掌——整條 codex-retained，不下放。
META_CODEX_RETAINED = {
    "general-provisions",
    "sovereignty",
    "amendment-and-protection",
    "separation-and-accountability",
    "interpretation-and-conflict",
    "closed-security",
    "anti-jailbreak-special-law",
}


def load_index() -> dict:
    if not INDEX_PATH.is_file():
        raise FileNotFoundError(f"index not found: {INDEX_PATH} (run build-runtime-rule-index.py first)")
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def classify(info: dict) -> str:
    """governance = 純治理維度；business = 純業務維度；mixed = 兩者兼有。

    fail-closed：任何命中治理維度的條文不得整條下放；
    mixed 條文下放業務內容、法典保留治理殼層。
    """
    dims = set(info.get("dimensions") or [])
    primary = info.get("primary_dimension") or "未分類"
    dims.add(primary)
    has_gov = bool(dims & GOVERNANCE_DIMS)
    has_biz = bool(dims - GOVERNANCE_DIMS - {"未分類"})
    if has_gov and has_biz:
        return "mixed"
    if has_gov:
        return "governance"
    return "business"


def build() -> dict:
    idx = load_index()
    provisions: dict = idx.get("provisions", {})
    rows: list[dict] = []
    counts = {"governance": 0, "business": 0, "mixed": 0}
    for pid in sorted(provisions):
        info = provisions[pid]
        primary = info.get("primary_dimension") or "未分類"
        section = info.get("section", "")
        kind = classify(info)
        counts[kind] += 1
        owning_unit = (
            "codex-retained"
            if kind == "governance" or section in META_CODEX_RETAINED
            else SECTION_TO_UNIT.get(section, "unassigned-review-needed")
        )
        rows.append({
            "provision_id": pid,
            "subject": info.get("subject", ""),
            "section": section,
            "primary_dimension": primary,
            "dimensions": info.get("dimensions", []),
            "kind": kind,
            "owning_unit": owning_unit,
            "owners": info.get("owners", []),
            "tier": info.get("tier", ""),
            "lifecycle": info.get("lifecycle", ""),
            "delegation_note": (
                "retain in codex (governance/boundary)" if kind == "governance"
                else "delegate business content, retain governance shell"
                if kind == "mixed"
                else "delegate to owning unit domain (successor, retain reference only)"
            ),
        })
    payload = {
        "schema": SCHEMA,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": "scripts/build-business-rule-delegation.py",
        "authority": "derived-rebuildable-non-authoritative; source is runtime-rule-index.json, which itself is derived from the PostgreSQL codex authority",
        "source": {
            "index": str(INDEX_PATH.relative_to(PROJECT_ROOT)),
            "codex_version": idx.get("source", {}).get("codex_version"),
            "index_content_sha256": idx.get("content_sha256"),
        },
        "rule": "Codex retains governance/authority/boundary only; business rules delegated to owning sovereign/module (blueprint §3.5 E). Fail-closed/governance provisions never delegated.",
        "governance_dimensions": sorted(GOVERNANCE_DIMS),
        "counts": {
            "provisions_total": len(rows),
            "governance": counts["governance"],
            "business": counts["business"],
            "mixed": counts["mixed"],
            "owning_unit_unassigned": sum(
                1 for r in rows
                if r["owning_unit"] == "unassigned-review-needed"
            ),
        },
        "rows": rows,
    }
    return payload


def write_outputs(payload: dict) -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT_JSON)
    # csv
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provision_id", "subject", "section", "primary_dimension", "kind", "owning_unit", "owners", "delegation_note"])
        for r in payload["rows"]:
            w.writerow([r["provision_id"], r["subject"], r["section"], r["primary_dimension"], r["kind"], r["owning_unit"], ";".join(r["owners"]), r["delegation_note"]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build business-rule delegation mapping (§3.5 E)")
    ap.add_argument("--summary", action="store_true", help="print counts")
    args = ap.parse_args(argv)
    payload = build()
    write_outputs(payload)
    if args.summary:
        print(json.dumps({"counts": payload["counts"], "out_json": str(OUT_JSON), "out_csv": str(OUT_CSV)}, ensure_ascii=False, indent=2))
    else:
        print(f"wrote {OUT_JSON} ({payload['counts']['governance']} governance / {payload['counts']['business']} business of {payload['counts']['provisions_total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
