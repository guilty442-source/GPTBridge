"""Validate successor contract/registry artifacts (A551-A590 batch 3, W4-3).

Each artifact in ``convergence/artifacts/{contracts,registries}`` must:

- parse and carry the required identity keys;
- define every declared field/column in ``field_definitions`` /
  ``column_definitions``;
- bind each definition to ``path#symbol`` or ``path`` references that exist
  under the project root (``#symbol`` additionally requires the symbol text
  to appear in the referenced file);
- list only existing ``implementation.enforcing_modules`` and
  ``evidence_paths``;
- keep ``status`` consistent with the ``gaps`` list:
  ``implemented`` requires zero gaps and full bindings.

Run:  python governance_rule/execution/convergence/contract_registry_validate.py
Writes ``audit/convergence/contract-registry-validation-<UTC>.json`` and exits
non-zero on any FAIL artifact.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = PROJECT_ROOT / "governance_rule" / "execution" / "convergence" / "artifacts"
REPORT_DIR = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "convergence"

_STATUSES = {"implemented", "partially-implemented", "declared-pending-implementation"}


def _resolve_binding(binding: str) -> tuple[str, str | None]:
    if "#" in binding:
        path, _, symbol = binding.partition("#")
        return path, symbol or None
    return binding, None


def validate_artifact(path: Path) -> dict:
    """Validate one artifact; returns a per-artifact result dict."""
    result: dict = {"artifact": path.name, "checks": [], "ok": True}

    def fail(msg: str) -> None:
        result["checks"].append(f"FAIL {msg}")
        result["ok"] = False

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report any parse error
        fail(f"json-parse: {exc}")
        return result

    kind = doc.get("kind")
    if kind not in ("contract", "registry"):
        fail(f"kind={kind!r}")
        return result
    code_key = "contract_code" if kind == "contract" else "registry_code"
    field_key = "fields" if kind == "contract" else "columns"
    defs_key = "field_definitions" if kind == "contract" else "column_definitions"

    for key in (code_key, "kind", "source_provision", "owner", "status", field_key):
        if key not in doc:
            fail(f"missing-key:{key}")

    status = doc.get("status")
    if status not in _STATUSES:
        fail(f"status={status!r}")

    declared = doc.get(field_key) or []
    defs = doc.get(defs_key) or {}
    if not declared:
        fail(f"{field_key}:empty")

    bound_count = 0
    for name in declared:
        entry = defs.get(name)
        if entry is None:
            fail(f"{name}:no-definition")
            continue
        bound_to = entry.get("bound_to") or []
        if not bound_to:
            continue  # declared gap
        bound_count += 1
        for binding in bound_to:
            rel, symbol = _resolve_binding(binding)
            target = PROJECT_ROOT / rel
            if not target.exists():
                fail(f"{name}:missing-path:{rel}")
                continue
            if symbol and target.is_file() and target.suffix != ".sqlite3":
                try:
                    if symbol not in target.read_text(encoding="utf-8", errors="replace"):
                        fail(f"{name}:missing-symbol:{rel}#{symbol}")
                except OSError as exc:
                    fail(f"{name}:unreadable:{rel}:{exc}")

    impl = doc.get("implementation") or {}
    for rel in impl.get("enforcing_modules") or []:
        if not (PROJECT_ROOT / rel).exists():
            fail(f"implementation:missing:{rel}")
    for rel in doc.get("evidence_paths") or []:
        if not (PROJECT_ROOT / rel).exists():
            fail(f"evidence:missing:{rel}")

    gaps = doc.get("gaps") or []
    unbound = [n for n in declared if not (defs.get(n) or {}).get("bound_to")]
    if sorted(unbound) != sorted(gaps):
        fail(f"gaps-mismatch:declared-unbound={unbound} gaps={gaps}")
    if status == "implemented" and gaps:
        fail("status=implemented but gaps non-empty")
    if status == "declared-pending-implementation" and bound_count:
        fail("status=pending but fields bound")

    result["bound_fields"] = bound_count
    result["declared_fields"] = len(declared)
    result["status"] = status
    if result["ok"]:
        result["checks"].append("PASS")
    return result


def main() -> int:
    results = [
        validate_artifact(p)
        for p in sorted(ARTIFACTS.glob("*/*.json"))
    ]
    passed = sum(1 for r in results if r["ok"])
    report = {
        "report": "contract-registry-validation/v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifact_total": len(results),
        "pass": passed,
        "fail": len(results) - passed,
        "status_counts": {},
        "results": results,
    }
    for r in results:
        s = r.get("status", "invalid")
        report["status_counts"][s] = report["status_counts"].get(s, 0) + 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORT_DIR / f"contract-registry-validation-{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report: {out}")
    print(f"artifacts={len(results)} pass={passed} fail={len(results) - passed} "
          f"statuses={report['status_counts']}")
    for r in results:
        if not r["ok"]:
            print(f"  FAIL {r['artifact']}: {[c for c in r['checks'] if c.startswith('FAIL')]}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
