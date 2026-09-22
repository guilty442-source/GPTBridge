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
SCHEMA = "business-rule-delegation/v1"


def load_index() -> dict:
    if not INDEX_PATH.is_file():
        raise FileNotFoundError(f"index not found: {INDEX_PATH} (run build-runtime-rule-index.py first)")
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def classify_primary(primary: str) -> str:
    # 治理/權責 與 安全與稽核 屬治理；其餘為業務（需下放）
    # 時限/預算 的 governance 性質：若同時命中治理維度則歸治理，否則業務；
    # 此處依 blueprint 原則「法典只保留治理、權責與邊界」，
    # 將 時限/預算 暫歸 governance（邊界），但標示為 boundary_governance 供複核。
    if primary in GOVERNANCE_DIMS:
        return "governance"
    return "business"


def build() -> dict:
    idx = load_index()
    provisions: dict = idx.get("provisions", {})
    rows: list[dict] = []
    counts = {"governance": 0, "business": 0}
    for pid in sorted(provisions):
        info = provisions[pid]
        primary = info.get("primary_dimension") or "未分類"
        kind = classify_primary(primary)
        counts[kind] += 1
        rows.append({
            "provision_id": pid,
            "subject": info.get("subject", ""),
            "primary_dimension": primary,
            "dimensions": info.get("dimensions", []),
            "kind": kind,
            "owners": info.get("owners", []),
            "tier": info.get("tier", ""),
            "lifecycle": info.get("lifecycle", ""),
            "delegation_note": (
                "retain in codex (governance/boundary)" if kind == "governance"
                else "delegate to sovereign/module domain (successor, retain reference only)"
            ),
        })
    payload = {
        "schema": SCHEMA,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": "scripts/build-business-rule-delegation.py",
        "authority": "derived-rebuildable-non-authoritative; source is runtime-rule-index.json, which itself is derived from governance_codex.sqlite3",
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
        w.writerow(["provision_id", "subject", "primary_dimension", "kind", "owners", "delegation_note"])
        for r in payload["rows"]:
            w.writerow([r["provision_id"], r["subject"], r["primary_dimension"], r["kind"], ";".join(r["owners"]), r["delegation_note"]])


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
