"""five_core_convergence_report.py — P9 五核心收斂報告產生器

唯讀投影：逐元件標示 PRIMARY / SHADOW / PARITY_VERIFIED /
OBSERVATION_PENDING / BLOCKED / LEGACY_RETIRED，附 release 觀察窗狀態與
未外移 Python 常駐工作。輸入全為既有受管產物；輸出為 derived artifact
（非規範來源——可重建、可安全刪除）。

Labels:
  LEGACY_RETIRED      — registry 已退役
  SHADOW              — native-shadow 在途（Python 仍 authoritative）
  PARITY_VERIFIED     — shadow 可接線維度全閉合（依 p4 gap matrix）
  OBSERVATION_PENDING — parity 已閉但 release 觀察窗未滿（P5 時程閘）
  BLOCKED             — 遷移受同儕認領／操作者／裁定閘控
  PRIMARY             — Python 常駐正典實作（retain 或待遷）
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAPPING = ROOT / "governance_rule/execution/convergence/five-core-mapping.json"
REGISTRY = ROOT / "governance_rule/execution/audit/architecture_registry.json"
P4_GAPS = ROOT / "governance_rule/execution/audit/convergence/p4-shadow-parity-gap-matrix-20260923.json"
P6_INVENTORY = ROOT / "governance_rule/execution/audit/convergence/p6-python-mechanical-inventory-20260923.json"
OUT = ROOT / "governance_rule/execution/convergence/five-core-convergence-report.json"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def build_report(root=ROOT):
    mapping = _load(root / MAPPING.relative_to(ROOT))
    registry = _load(root / REGISTRY.relative_to(ROOT)) or {}
    p4 = _load(root / P4_GAPS.relative_to(ROOT)) or {}
    p6 = _load(root / P6_INVENTORY.relative_to(ROOT)) or {}

    retired = {c["component_id"] for c in registry.get("components", [])
               if c.get("status") == "retired"}
    shadow_modules = {s["module"]: s for s in mapping.get("shadow_modules", [])}
    gap_matrix = {g.get("module"): g for g in p4.get("modules", p4.get("matrix", [])) if isinstance(g, dict)}
    blocked_surfaces = {s.get("surface") or s.get("path"): s for s in p6.get("surfaces", []) if s.get("blocked_by")}

    components = []
    for core_id, core in mapping.get("cores", {}).items():
        for proc in core.get("processes", []):
            cid = proc["component_id"]
            entry = {
                "component_id": cid,
                "core": core_id,
                "runtime_form": proc.get("runtime_form"),
                "migration_state": proc.get("migration_state"),
            }
            path_mod = (proc.get("physical_path") or "").replace("\\", "/").rsplit("/", 1)[-1].replace(".py", "")
            if cid in retired or proc.get("implementation") == "retired":
                entry["status"] = "LEGACY_RETIRED"
            elif proc.get("implementation") == "shadow" or cid in shadow_modules or path_mod in shadow_modules:
                gaps = gap_matrix.get(cid) or gap_matrix.get(proc.get("physical_path", "").rsplit("/", 1)[-1].replace(".py", ""))
                wired_open = [d for d in (gaps or {}).get("open_dimensions", []) if not str(d).startswith("modeling")]
                if wired_open:
                    entry["status"] = "SHADOW"
                    entry["open_dimensions"] = wired_open
                else:
                    entry["status"] = "OBSERVATION_PENDING"
                    entry["note"] = "wireable parity closed; release observation windows pending (P5)"
            elif cid in blocked_surfaces or proc.get("blocked_by"):
                entry["status"] = "BLOCKED"
                entry["blocked_by"] = proc.get("blocked_by") or blocked_surfaces.get(cid, {}).get("blocked_by")
            else:
                entry["status"] = "PRIMARY"
            components.append(entry)

    counts = {}
    for c in components:
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    unmigrated = [s for s in p6.get("surfaces", []) if s.get("disposition") not in ("native-shadow", "csharp-owned")]

    # shadow surfaces are sub-surfaces of boot-core (physical_path main-system/src-core),
    # not standalone component rows — report them per-module.
    shadow_surfaces = []
    for mod, s in sorted(shadow_modules.items()):
        gaps = gap_matrix.get(mod, {})
        wired_open = [d for d in gaps.get("open_dimensions", [])
                      if not str(d).startswith("modeling")]
        shadow_surfaces.append({
            "module": mod,
            "owning_component": s.get("owning_component"),
            "core": s.get("core"),
            "mode": s.get("mode"),
            "status": "SHADOW" if wired_open else "OBSERVATION_PENDING",
            "open_dimensions": wired_open,
            "note": "wireable parity closed; release observation windows pending (P5)"
                    if not wired_open else None,
        })

    return {
        "schema": "five-core-convergence-report/v1",
        "derived_from": [str(p.relative_to(ROOT)) for p in (MAPPING, REGISTRY, P4_GAPS, P6_INVENTORY)],
        "status_counts": counts,
        "components": components,
        "shadow_surfaces": shadow_surfaces,
        "release_observation_windows": {
            "required": 2,
            "completed": 0,
            "note": "P5 promotion gate: two release observation windows on wired parity; window accumulation pending INT-10 close",
        },
        "python_residency_not_migrated": [
            {"surface": s.get("surface") or s.get("path"), "disposition": s.get("disposition"),
             "blocked_by": s.get("blocked_by")}
            for s in unmigrated
        ],
    }


if __name__ == "__main__":
    report = build_report()
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"components={len(report['components'])} counts={report['status_counts']}")
    print(f"unmigrated-python-surfaces={len(report['python_residency_not_migrated'])}")
