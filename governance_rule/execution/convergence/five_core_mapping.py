"""Five-core current-state mapping (P3).

Read-only projection joining three governed sources:

- architecture registry (canonical owner / process surface / unit lineage /
  python_residency disposition — G100 increments)
- ``main-system/config/native-shadow.json`` (§10.65 dual-track shadow modes)
- successor contract artifacts (versioned contract bindings via
  ``implementation.enforcing_modules`` path containment)

Output: ``five-core-mapping.json`` next to this module. Rebuildable; never
mutates the codex or the registry.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "governance_rule/execution/audit/architecture_registry.json"
SHADOW = ROOT / "main-system/config/native-shadow.json"
CONTRACTS = ROOT / "governance_rule/execution/convergence/artifacts/contracts"
OUTPUT = Path(__file__).with_name("five-core-mapping.json")

CORES = ("decision", "permission", "runtime", "automation", "xingcheng-assistant")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_mapping() -> dict:
    registry = _load(REGISTRY)
    shadow = _load(SHADOW).get("components") or {}
    components = registry.get("components") or []

    # component_id -> physical prefix for contract-path containment.
    prefixes = {
        str(c.get("component_id")): str(c.get("physical_path") or "")
        for c in components
        if isinstance(c, dict)
    }

    def owner_of_path(path: str) -> str:
        best = ""
        for cid, prefix in prefixes.items():
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                if len(prefix) > len(best):
                    best = cid
        return best

    # contract code -> bound component ids (deduped, ordered).
    contract_bindings: dict[str, list[str]] = {}
    for artifact in sorted(CONTRACTS.glob("*.json")):
        try:
            data = _load(artifact)
        except (OSError, json.JSONDecodeError):
            continue
        code = str(data.get("contract_code") or artifact.stem)
        modules = (
            (data.get("implementation") or {}).get("enforcing_modules") or []
        )
        bound = sorted(
            {
                cid
                for m in modules
                for cid in [owner_of_path(str(m))]
                if cid
            }
        )
        contract_bindings[code] = bound

    # component -> contracts it enforces.
    component_contracts: dict[str, list[str]] = {}
    for code, cids in contract_bindings.items():
        for cid in cids:
            component_contracts.setdefault(cid, []).append(code)

    # reverse dependency graph -> consumers.
    consumers: dict[str, list[str]] = {}
    for c in components:
        cid = str(c.get("component_id"))
        for dep in c.get("dependencies") or ():
            consumers.setdefault(str(dep), []).append(cid)

    cores = registry.get("five_cores") or {}
    mapping: dict[str, dict] = {}
    for core in CORES:
        entry = cores.get(core) or {}
        processes = []
        for cid in entry.get("processes") or ():
            comp = next(
                (c for c in components if c.get("component_id") == cid), {}
            )
            processes.append(
                {
                    "component_id": cid,
                    "canonical_owner": comp.get("owner_sovereign"),
                    "implementation": (
                        "retired"
                        if comp.get("lifecycle") == "retired"
                        else "primary"
                    ),
                    "runtime_form": comp.get("runtime_form"),
                    "resident_lifecycle": comp.get("lifecycle"),
                    "physical_path": comp.get("physical_path") or None,
                    "external_locator": comp.get("external_locator") or None,
                    "unit": comp.get("unit") or None,
                    "contracts": component_contracts.get(cid, []),
                    "consumers": sorted(consumers.get(cid, [])),
                    "migration_state": comp.get("python_residency")
                    or ("native" if comp.get("runtime_form") != "python-process" else "undeclared"),
                }
            )
        mapping[core] = {
            "sovereign_id": entry.get("sovereign_id"),
            "unit": entry.get("unit"),
            "capabilities": entry.get("capabilities") or [],
            "processes": processes,
            "dependencies": entry.get("dependencies") or [],
        }

    # Shadow modules are file-level dual-track entries inside components —
    # attach them to their owning component's core.
    shadow_entries = []
    for module_name, spec in sorted(shadow.items()):
        py_rel = f"main-system/src-core/core_system/{module_name}.py"
        owner = owner_of_path(py_rel) or "main-system"
        shadow_entries.append(
            {
                "module": module_name,
                "mode": spec.get("mode"),
                "owning_component": owner,
                "path": py_rel,
            }
        )
    for entry in shadow_entries:
        owner = entry["owning_component"]
        for core, m in mapping.items():
            if any(p["component_id"] == owner for p in m["processes"]):
                entry["core"] = core
                break
        else:
            entry["core"] = None
    return {
        "schema": "gptbridge-five-core-mapping/v1",
        "generated_from": [
            "architecture_registry.json",
            "native-shadow.json",
            "convergence/artifacts/contracts",
        ],
        "cores": mapping,
        "shadow_modules": shadow_entries,
        "unbound_contracts": [
            code for code, cids in contract_bindings.items() if not cids
        ],
    }


def main() -> int:
    OUTPUT.write_text(
        json.dumps(build_mapping(), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
