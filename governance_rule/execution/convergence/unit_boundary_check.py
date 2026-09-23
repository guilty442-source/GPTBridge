"""unit_boundary_check.py — P10 跨單元介面稽核（derived check，唯讀）

單次遍歷掃描 Python 匯入邊，以最長 physical_path 前綴判定檔案歸屬元件，
比對 five-core-mapping 宣告的 consumers 邊。跨單元且未宣告的匯入
記為 violation（候選收斂點），非權威裁決——輸出供搬移排序參考。

已知限制：動態 import（importlib/字串載入）與 re-export 不解析；
測試目錄與 .venv 排除。
"""
import ast
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAPPING = ROOT / "governance_rule/execution/convergence/five-core-mapping.json"
OUT = ROOT / "governance_rule/execution/convergence/unit-boundary-report.json"
EXCLUDE_DIRS = {".venv", "tests", "test", "__pycache__", "node_modules", ".git",
                ".worktrees", "runtime", "data", "logs"}
SCAN_ROOTS = ["main-system/src-core", "governance_rule", "shared-layer/src",
              "Standalone tools"]


def _top_packages(base: Path):
    pkgs = set()
    if not base.is_dir():
        return pkgs
    for child in base.iterdir():
        if child.is_dir() and (child / "__init__.py").exists():
            pkgs.add(child.name)
        elif child.suffix == ".py" and child.name != "__init__.py":
            pkgs.add(child.stem)
    return pkgs


def build_report(root=ROOT):
    mapping = json.loads((root / MAPPING.relative_to(ROOT)).read_text(encoding="utf-8"))
    components = []
    for core_id, core in mapping.get("cores", {}).items():
        for proc in core.get("processes", []):
            proc = dict(proc)
            proc["core"] = core_id
            components.append(proc)

    comp_by_id = {c["component_id"]: c for c in components}
    # longest physical_path prefix -> component (file ownership)
    path_owner = sorted(
        ((str(root / c["physical_path"]).replace("/", os.sep).lower(), c["component_id"])
         for c in components if c.get("physical_path")),
        key=lambda t: -len(t[0]))

    pkg_owner: dict[str, list[str]] = {}
    for c in sorted(components, key=lambda x: len(x.get("physical_path") or "")):
        pp = c.get("physical_path")
        if not pp or c.get("runtime_form") not in ("python-process", "package"):
            continue
        for pkg in _top_packages(root / pp):
            pkg_owner.setdefault(pkg, [])
            if c["component_id"] not in pkg_owner[pkg]:
                pkg_owner[pkg].append(c["component_id"])

    def owner_of(path: Path):
        p = str(path).lower()
        for prefix, cid in path_owner:
            if p.startswith(prefix):
                return cid
        return None

    edges, violations, seen = {}, [], set()
    for sr in SCAN_ROOTS:
        base = root / sr
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                f = Path(dirpath) / fn
                src_id = owner_of(f)
                if not src_id:
                    continue
                src_unit = comp_by_id[src_id].get("unit")
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        names = [node.module.split(".")[0]]
                    for top in names:
                        candidates = pkg_owner.get(top) or []
                        if src_id in candidates:
                            continue  # same-component module (e.g. `import main`)
                        if len(candidates) > 1:
                            # Same-named module across units: prefer a
                            # same-unit candidate; skip when truly ambiguous.
                            same_unit = [
                                c for c in candidates
                                if comp_by_id[c].get("unit") == src_unit
                            ]
                            if len(same_unit) == 1:
                                candidates = same_unit
                            elif not same_unit:
                                continue
                        if not candidates:
                            continue
                        tgt_id = candidates[0]
                        if tgt_id == src_id:
                            continue
                        edge = f"{src_id}->{tgt_id}"
                        edges[edge] = edges.get(edge, 0) + 1
                        tgt = comp_by_id[tgt_id]
                        if (tgt.get("unit") != src_unit
                                and src_id not in (tgt.get("consumers") or [])
                                and edge not in seen):
                            seen.add(edge)
                            violations.append({
                                "edge": edge,
                                "src_unit": src_unit,
                                "tgt_unit": tgt.get("unit"),
                                "via_module": top,
                                "example_file": str(f.relative_to(root)),
                            })

    return {
        "schema": "unit-boundary-report/v1",
        "note": "derived check; dynamic imports unresolvable; violations are convergence candidates, not verdicts",
        "edge_count": len(edges),
        "cross_unit_edges": len(violations),
        "violations": violations,
    }


if __name__ == "__main__":
    report = build_report()
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"edges={report['edge_count']} cross-unit={report['cross_unit_edges']}")
    for v in report["violations"]:
        print(f"  VIOL {v['edge']} via {v['via_module']} ({v['src_unit']}->{v['tgt_unit']})")
