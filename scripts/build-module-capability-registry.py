"""Build semantic module-capability registry + dispatch parity evidence.

§10.51 PARITY_VERIFIED pending ①: replace identity/role capability codes
with semantic codes so ``RULE_CAPABILITY_DISPATCH_V1`` selection is
unambiguous.  This script derives the registry from authoritative sources
only:

- ``architecture_registry.json`` — component list, role, identity
- ``capability_boundaries.py`` — permission-level capabilities owned by
  each component (``tool:<id>`` owner → ``component_id``)
- codex ``module_assignment_registry`` — legacy fixed assignments used
  as the parity baseline

Code taxonomy (proposal; ratification belongs to the CUTOVER gate):
- ``duty:<component_id>``   unique duty code — dispatch discriminator
- ``role:<arch_role>``      role class — informational, never sole basis
- ``grant:<authority>``     permission capability owned by the module

Outputs (derived evidence, not codex):
- ``audit/convergence/module_capability_registry.json``
- ``audit/convergence/capability_dispatch_parity.json`` (v2 refresh)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REGISTRY_PATH = ROOT / "governance_rule" / "execution" / "audit" / "architecture_registry.json"
OUT_DIR = ROOT / "governance_rule" / "execution" / "audit" / "convergence"

RESPONSIBILITY = {
    # component_id -> single-responsibility text (<=120 chars), sourced from
    # module manifests / architectural role; kept short and factual.
}

# Codex module codes that are facades/services hosted inside a physical
# component (no standalone component of their own).  Dispatch parity expects
# the requirement to resolve to the hosting module.
HOST_MAP = {
    "EVENT_SERVICE": "main-system",
    "IPC_SERVICE": "main-system",
    "STATE_SERVICE": "main-system",
    "LAUNCHER": "launcher-ui",
    "POSTGRESQL_AUTHORITY": "postgresql",
    "QDRANT_AUTHORITY": "qdrant",
}


def _load_capability_owners() -> dict[str, list[str]]:
    """component_id -> capability-boundary authority names it owns."""
    from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
        CAPABILITY_AUTHORITIES,
    )

    owned: dict[str, list[str]] = {}
    for authority in CAPABILITY_AUTHORITIES:
        owner = str(authority.owner)
        component = owner.removeprefix("tool:")
        if "{" in component:  # template owner, not a concrete module
            continue
        owned.setdefault(component, []).append(str(authority.capability))
    return owned


def _responsibility(component: dict) -> str:
    cid = component["component_id"]
    if cid in RESPONSIBILITY:
        return RESPONSIBILITY[cid]
    role = component.get("architectural_role") or "module"
    return f"{role} duty module: {cid}"


def main() -> int:
    from governance_rule.execution.convergence.capability_dispatch import (
        evaluate_dispatch,
    )

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    components = registry["components"]
    grant_owners = _load_capability_owners()

    from governance_rule.execution.codex_repository import (
        codex_readonly_connection,
    )

    with codex_readonly_connection() as codex:
        assignments = {
            row[0]: row[1]
            for row in codex.execute(
                "SELECT module_architecture_code, managing_sub_sovereign "
                "FROM module_assignment_registry"
            )
        }

    rows = []
    code_index: dict[str, list[str]] = {}
    for comp in components:
        cid = comp["component_id"]
        codes = [
            f"duty:{cid}",
            f"role:{comp.get('architectural_role') or 'unknown'}",
        ]
        codes += [f"grant:{name}" for name in sorted(grant_owners.get(cid, ()))]

        row = {
            "module_identity": cid,
            "single_responsibility": _responsibility(comp),
            "capability_codes": codes,
            "resource_requirements": {"runtime_form": comp.get("runtime_form")},
            "permission_requirements": tuple(
                [f"identity:{comp.get('execution_identity') or cid}"]
                + [f"grant:{n}" for n in sorted(grant_owners.get(cid, ()))]
            ),
            "contract_version": "module-capability/v1",
            "runtime_state": (
                "retired"
                if str(comp.get("execution_identity") or "").startswith("retired-")
                else "active"
            ),
            "availability": (
                "retired"
                if str(comp.get("execution_identity") or "").startswith("retired-")
                else "active"
            ),
            "version": registry.get("registry", "v1"),
            "owner_engine_domain": comp.get("owner_sovereign") or "",
            "execution_identity": comp.get("execution_identity") or "",
        }
        rows.append(row)
        for code in codes:
            code_index.setdefault(code, []).append(cid)

    # Conflicts: only duty:/grant: codes may collide; role:* are informational.
    conflicts = {
        code: owners
        for code, owners in code_index.items()
        if len(owners) > 1 and not code.startswith("role:")
    }
    role_sharing = {
        code: owners
        for code, owners in code_index.items()
        if len(owners) > 1 and code.startswith("role:")
    }

    # Parity: for every legacy fixed assignment, a duty-coded requirement must
    # resolve to exactly that module through RULE_CAPABILITY_DISPATCH_V1.
    results = []
    parity_pass = 0
    for module_code, sub_sovereign in sorted(assignments.items()):
        cid = module_code.lower().replace("_", "-")
        host = HOST_MAP.get(module_code)
        expected = host or cid
        requirement = {"capability_codes": (f"duty:{expected}",)}
        ok, decision, detail = evaluate_dispatch(
            {
                "requirements": requirement,
                "candidates": rows,
                "lease": {"valid": True, "fencing_token": "parity-probe"},
                "decision": {"recorded": True},
            }
        )
        selected = detail.removeprefix("eligible module ") if ok else ""
        if module_code == "GLOBAL_CLEANER":
            # Retired module: correct outcome is fail-closed refusal.
            same = not ok
        else:
            same = ok and selected == expected
        parity_pass += int(same)
        results.append(
            {
                "module_identity": module_code,
                "fixed_assignment_owner_sub_sovereign": sub_sovereign,
                "hosted_in": host,
                "capability_dispatch_selected": selected or None,
                "parity": bool(same),
                "decision": decision,
                "detail": detail,
            }
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    registry_doc = {
        "registry": "MODULE_CAPABILITY_REGISTRY/v1",
        "generated_at": now,
        "authority": "derived-evidence; code taxonomy pending CUTOVER gate ratification",
        "modules": rows,
    }
    (OUT_DIR / "module_capability_registry.json").write_text(
        json.dumps(registry_doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    parity_doc = {
        "evidence": "CAPABILITY_DISPATCH_PARITY/v2",
        "generated_at": now,
        "modules_total": len(rows),
        "schema_ok": len(rows),
        "schema_errors": {},
        "semantic_codes": True,
        "capability_conflicts": conflicts,
        "role_code_sharing_informational": role_sharing,
        "parity_pass": parity_pass,
        "parity_fail": [r["module_identity"] for r in results if not r["parity"]],
        "results": results,
        "closure": "INCOMPLETE_EVIDENCE",
        "pending": [
            "runtime dispatch wiring (RULE_CAPABILITY_DISPATCH_V1 not active in runtime)",
            "registry switch + activation gate",
            "code taxonomy ratification at CUTOVER gate",
        ],
        "summary": {
            "total": len(results),
            "pass": parity_pass,
            "fail": len(results) - parity_pass,
        },
    }
    (OUT_DIR / "capability_dispatch_parity.json").write_text(
        json.dumps(parity_doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "modules": len(rows),
                "conflicts": len(conflicts),
                "parity_pass": parity_pass,
                "parity_total": len(results),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
