"""E③ codex x business-rule conflict scan.

Extracts business-rule declarations from tool manifests and compares
them against the codex authority layer (capability boundaries, tool
routes, identity registry, module assignment registry).  Every finding
is classified:

  conflict  — real conflict (double ownership, undeclared authority,
              boundary owner mismatch); must not dual-track, needs an
              amendment or implementation fix
  overlap   — same surface declared in two places without contradiction
              (e.g. retired-tool residue, informational duplicates)
  naming    — same concept under different names across layers

The scan is evidence-only: it never mutates governance state.
Output: governance_rule/execution/audit/convergence/
        codex-business-rule-conflicts.json
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = (
    ROOT / "governance_rule" / "execution" / "audit" / "convergence"
    / "codex-business-rule-conflicts.json"
)

AUTHORITY_CLAIM_RE = re.compile(
    r"唯一權威|sole[- ]authority|authoritative[- ]codex|凌駕|"
    r"override[s]?\b.*codex|codex.*overrid",
    re.IGNORECASE,
)


def _load_manifests() -> dict[str, dict]:
    manifests: dict[str, dict] = {}
    for path in sorted((ROOT / "Standalone tools").rglob("manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # dataset/artifact manifests (corpus-*, dataset snapshots) are not
        # tools — a tool manifest declares an id plus runtime/permissions
        if not data.get("id") or not (
            data.get("runtime") or data.get("permissions") or data.get("capabilities")
        ):
            continue
        tool_id = str(data["id"])
        data["_path"] = str(path.relative_to(ROOT))
        manifests[tool_id] = data
    return manifests


def _is_retired(manifest: dict) -> bool:
    lifecycle = manifest.get("lifecycle") or {}
    return (
        lifecycle.get("status") == "retired"
        or manifest.get("status") == "retired"
        or manifest.get("enabled") is False
    )


def _codex_module_assignments() -> dict[str, dict]:
    # A173: the sqlite predecessor is retired — read the PostgreSQL
    # authority through the governed repository interface.
    from governance_rule.execution.codex_repository import (
        codex_readonly_connection,
    )

    with codex_readonly_connection() as db:
        rows = db.execute(
            "SELECT module_architecture_code, managing_sub_sovereign, "
            "decision_authority, permission_authority, review_authority, "
            "execution_identity, status FROM module_assignment_registry"
        ).fetchall()
    return {
        r[5]: {
            "module": r[0],
            "managing_sub_sovereign": r[1],
            "decision_authority": r[2],
            "permission_authority": r[3],
            "status": r[6],
        }
        for r in rows
    }


def main() -> int:
    from governance_rule.permission_directory.registries.permissions.capability_boundaries import (  # noqa: E501
        capability_boundary_snapshot,
    )
    from governance_rule.permission_directory.registries.permissions.identity_groups import (  # noqa: E501
        identity_group_snapshot,
    )
    from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E501
        AI_ROUTE_COMMANDS,
    )

    manifests = _load_manifests()
    boundaries = {
        c.capability: c.owner for c in capability_boundary_snapshot()[0]
    }
    identities = identity_group_snapshot()
    registered_tools = {
        ident.bound_tool_id: ident.actor
        for ident in identities.identities
        if getattr(ident, "bound_tool_id", None)
    }
    assignments = _codex_module_assignments()

    findings: list[dict] = []

    def record(axis: str, kind: str, subject: str, detail: str) -> None:
        findings.append(
            {"axis": axis, "class": kind, "subject": subject,
             "detail": detail}
        )

    # --- axis 1: capability key collisions across manifests ----------
    declarers: dict[str, list[str]] = {}
    for tool_id, manifest in manifests.items():
        for cap in (manifest.get("capabilities") or {}):
            declarers.setdefault(cap, []).append(tool_id)
    def _declared_owner(entry: object) -> str | None:
        """Resolve a manifest capability entry's declared owner, if any.

        Entries that merely describe the tool's own connectivity
        (transport/peers/entry_gateway) carry no ownership claim.
        """
        if not isinstance(entry, dict):
            return None
        for key in ("source_owner", "owner"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
        for key, value in entry.items():
            if (
                key.startswith("shared_")
                and key.endswith("_source_root")
                and isinstance(value, str)
                and value
            ):
                return value
        return None

    for cap, tools in sorted(declarers.items()):
        if len(tools) < 2:
            continue
        active = [t for t in tools if not _is_retired(manifests[t])]
        retired = [t for t in tools if _is_retired(manifests[t])]
        owners = {
            t: _declared_owner(manifests[t]["capabilities"][cap])
            for t in tools
        }
        distinct = {o for o in owners.values() if o}
        # A declaration is a *shared-ownership* claim only when the
        # named owner is outside the declarer's own scope (self /
        # host / companion owner / the entry's declared gateway).
        # Self-scoped source_owner values describe per-tool profiles
        # of a shared domain, not claims on the shared key.
        def _self_scoped(tool: str) -> bool:
            claim = owners[tool]
            if not claim:
                return True
            manifest = manifests[tool]
            entry = manifest["capabilities"][cap]
            allowed = {tool, manifest.get("host_tool_id"),
                       manifest.get("companion_owner"),
                       entry.get("entry_gateway") if isinstance(entry, dict) else None}
            return claim in {a for a in allowed if a}
        contested = [t for t in active if not _self_scoped(t)]
        if active and retired:
            record(
                "capability-key", "overlap", cap,
                f"declared by retired {sorted(retired)} and active "
                f"{sorted(active)} — retired declaration is residue; "
                "active tool is the live owner",
            )
        elif len(active) > 1:
            if len(distinct) == 1:
                record(
                    "capability-key", "overlap", cap,
                    f"shared-domain key across {sorted(active)}; all "
                    f"declarations agree owner={sorted(distinct)[0]}",
                )
            elif contested:
                record(
                    "capability-key", "conflict", cap,
                    f"declared by multiple ACTIVE tools {sorted(active)} "
                    f"with DISAGREEING owner claims {owners} — "
                    f"{sorted(contested)} claim ownership outside their "
                    "own scope; double business-rule ownership",
                )
            else:
                record(
                    "capability-key", "overlap", cap,
                    f"shared-domain key across {sorted(active)}; "
                    "declarations are per-tool profiles (self-scoped "
                    "source_owner), no shared-ownership conflict",
                )
        else:
            record(
                "capability-key", "overlap", cap,
                f"declared only by retired tools {sorted(retired)}",
            )

    # --- axis 2: manifest caps that are also boundary capabilities ---
    for tool_id, manifest in manifests.items():
        for cap in (manifest.get("capabilities") or {}):
            if cap not in boundaries:
                continue
            owner = boundaries[cap]
            expected = f"tool:{tool_id}"
            host = manifest.get("host_tool_id") or manifest.get(
                "companion_owner"
            )
            allowed = {expected, f"tool:{host}" if host else ""}
            if owner not in allowed:
                kind = "overlap" if _is_retired(manifest) else "conflict"
                record(
                    "capability-owner", kind, cap,
                    f"manifest {tool_id} declares '{cap}' but boundary "
                    f"owner is '{owner}'",
                )

    # --- axis 3: route actors must be registered identities ----------
    for (requester, target) in sorted(AI_ROUTE_COMMANDS):
        for role, tool in (("requester", requester), ("target", target)):
            if tool not in registered_tools:
                record(
                    "route", "conflict", f"{requester}->{target}",
                    f"{role} '{tool}' has a route but no registered "
                    "identity",
                )
            elif tool not in manifests:
                record(
                    "route", "overlap", f"{requester}->{target}",
                    f"{role} '{tool}' is a registered identity without "
                    "a tool manifest (internal identity)",
                )
            elif _is_retired(manifests[tool]):
                record(
                    "route", "conflict", f"{requester}->{target}",
                    f"{role} '{tool}' has an active route but its "
                    "manifest is retired/disabled",
                )

    # --- axis 4: authority-claiming language in business rules -------
    for tool_id, manifest in manifests.items():
        text = json.dumps(manifest, ensure_ascii=False)
        for match in AUTHORITY_CLAIM_RE.finditer(text):
            record(
                "authority-claim", "conflict", tool_id,
                f"manifest asserts authority ('{match.group(0)}') — "
                "business rules are subordinate; codex is the sole "
                "authority",
            )

    # --- axis 5: execution_identity coverage ---------------------------
    for tool_id, manifest in manifests.items():
        if _is_retired(manifest):
            continue
        code = re.sub(r"[^A-Z0-9]+", "_", tool_id.upper())
        if code not in assignments and tool_id not in {
            i.split("/")[-1] for i in registered_tools.values()
        }:
            record(
                "module-assignment", "overlap", tool_id,
                f"active tool has no module_assignment_registry row "
                f"(execution_identity '{code}')",
            )

    # --- axis 6: deadline/budget numeric rules vs codex metadata -----
    def _codex_metadata() -> dict[str, str]:
        from governance_rule.execution.codex_repository import (
            codex_readonly_connection,
        )

        with codex_readonly_connection() as db:
            return {
                str(k): str(v)
                for k, v in db.execute("SELECT key, value FROM metadata")
            }

    codex_meta = _codex_metadata()
    budget_key_re = re.compile(
        r"deadline|timeout|budget|limit|max|cap|threshold|quota",
        re.IGNORECASE,
    )
    codex_budget = {
        k: v for k, v in codex_meta.items() if budget_key_re.search(k)
    }

    GENERIC_TOKENS = {
        "ms", "s", "the", "a", "of", "per", "max", "min", "limit",
        "timeout", "deadline", "budget", "quota", "cap", "threshold",
        "seconds", "minutes", "hours", "days", "count", "size",
        "interval", "global", "default", "idle", "value", "number",
        "total", "high", "low", "soft", "hard", "time",
    }

    def _tokens(key: str) -> set[str]:
        return {
            t for t in re.split(r"[^a-z0-9]+", key.lower())
            if t and t not in GENERIC_TOKENS
        }

    def _iter_numeric(obj: object, prefix: str):
        if isinstance(obj, dict):
            for key, value in obj.items():
                yield from _iter_numeric(
                    value, f"{prefix}.{key}" if prefix else str(key)
                )
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            yield prefix, obj

    def _config_files() -> list[Path]:
        roots = [
            ROOT / "main-system" / "config",
            ROOT / "main-system" / "runtime" / "settings",
            ROOT / "Standalone tools" / "local-model"
            / "runtime" / "settings",
        ]
        files: list[Path] = []
        for root in roots:
            if root.is_dir():
                files.extend(
                    sorted(root.glob("*.json"))
                )
        return files

    scanned_configs = 0
    for cfg_path in _config_files():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        scanned_configs += 1
        rel = str(cfg_path.relative_to(ROOT))
        for key_path, value in _iter_numeric(cfg, ""):
            leaf = key_path.split(".")[-1]
            if not budget_key_re.search(leaf):
                continue
            leaf_tokens = _tokens(leaf)
            if not leaf_tokens:
                continue
            # Best-match: the codex key whose distinctive tokens best
            # cover the leaf's.  Require either >=2 shared distinctive
            # tokens or full coverage of the leaf's tokens.
            best: tuple[float, str] | None = None
            for codex_key in codex_budget:
                codex_tokens = _tokens(codex_key)
                shared = leaf_tokens & codex_tokens
                if not shared:
                    continue
                if len(shared) < 2 and shared != leaf_tokens:
                    continue
                score = len(shared) / max(len(codex_tokens), 1)
                if best is None or score > best[0]:
                    best = (score, codex_key)
            if best is None:
                continue
            codex_key = best[1]
            codex_value = codex_budget[codex_key]
            try:
                codex_num = float(codex_value)
            except ValueError:
                continue
            if float(value) != codex_num:
                record(
                    "deadline-budget", "conflict",
                    f"{rel}:{key_path}",
                    f"business rule '{leaf}'={value} contradicts "
                    f"codex metadata '{codex_key}'={codex_value}",
                )
            else:
                record(
                    "deadline-budget", "overlap",
                    f"{rel}:{key_path}",
                    f"'{leaf}' duplicates codex '{codex_key}' "
                    f"(same value {value})",
                )

    # --- axis 7: authority-claiming language in configs/contracts ----
    for cfg_path in _config_files():
        try:
            text = cfg_path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(cfg_path.relative_to(ROOT))
        for match in AUTHORITY_CLAIM_RE.finditer(text):
            record(
                "authority-claim", "conflict", rel,
                f"config asserts authority ('{match.group(0)}') — "
                "business rules are subordinate; codex is the sole "
                "authority",
            )

    summary = {
        "conflict": sum(1 for f in findings if f["class"] == "conflict"),
        "overlap": sum(1 for f in findings if f["class"] == "overlap"),
        "naming": sum(1 for f in findings if f["class"] == "naming"),
    }
    report = {
        "schema": "codex-business-rule-conflict-scan/v1",
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "sources": {
            "manifests": len(manifests),
            "boundary_capabilities": len(boundaries),
            "registered_tool_identities": len(registered_tools),
            "routes": len(AI_ROUTE_COMMANDS),
            "module_assignments": len(assignments),
            "config_files": scanned_configs,
            "codex_budget_keys": len(codex_budget),
        },
        "summary": summary,
        "findings": findings,
        "note": (
            "evidence-only scan; business rules are subordinate to the "
            "codex (sole authority); conflicts must converge via "
            "amendment or implementation fix, never dual-track"
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "report": str(OUT),
        "summary": summary,
        "conflicts": [
            f for f in findings if f["class"] == "conflict"
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
