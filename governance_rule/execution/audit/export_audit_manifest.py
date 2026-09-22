"""Export the governed audit-check manifest for the native audit engine.

``star-audit-manifest/v1`` — the *data* driving
``native/audit/audit_engine.cpp`` (§1.1 權限核心 C/C++ 審計路徑；P0-9).

The governed Python tooling remains the **source of truth for what must
be checked**: protected-source lists come from the live policy/directory
snapshots, forbidden paths from the audit registry, pollution targets
from the codex tree.  The engine only executes — it never decides which
files are protected.

Shadow semantics (same dual-track as the E1 execution prototypes):

- checks reducible to static file operations are emitted with native
  ``kind`` (``file-exists`` / ``file-not-exists`` / ``file-readonly`` /
  ``file-contains`` / ``text-no-pollution`` / ``json-parses`` /
  ``glob-min-count``) — the engine verifies them directly;
- every Python ``check_*`` function not fully reducible is emitted as a
  ``delegated`` record — explicit, counted, never silently dropped;
- regenerating after any governance-data change is the cache-invalidation
  policy (法典變更 → manifest 重算，P0-9 ④).

Usage::

    python -m governance_rule.execution.audit.export_audit_manifest \
        --root E:/GPTBridge [--out <path>]
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
import sys
import time
from pathlib import Path

# Check modules mirrored by audit_checks.audit_runtime_governance.
_CHECK_MODULES = (
    "audit_artifacts",
    "audit_codex_integrity",
    "audit_authority",
    "audit_activation",
    "audit_architecture",
    "audit_directories",
    "audit_formal_rules",
    "audit_manifests",
    "audit_protected",
    "audit_contract_axes",
    "audit_runtime_contracts",
)

# Python check functions fully or partially covered by native checks
# emitted below.  Everything else lands in ``delegated_python`` — visible
# and counted, never silently skipped.
_NATIVE_COVERED = frozenset({
    "check_protected_sources",      # exists+readonly (ast/sqlite delegated)
    "check_forbidden_legacy",       # file-not-exists
    "check_codex_consistency",      # pollution part only (delegated row kept)
    "check_codex_mirror_quality",   # mirror part-file pollution (delegated row kept)
    "check_runtime_contracts",      # contract JSON files parse
    # P0-9 second expansion: static file/marker checks reduced natively.
    "check_metadata_contract",
    "check_shared_layer_structure",
    "check_sqlite_template",
    "check_embedded_browser",
    "check_orphan_scanner",
    "check_git_tiers",              # classify()==3 semantic stays delegated
    "check_release_manifest_file",
    "check_release_manifest_module",
    "check_architecture_sources",
    "check_main_system_source",
})


def _iter_python_check_names(audit_pkg: object) -> list[str]:
    """All ``check_*`` function names across the audit modules."""
    names: list[str] = []
    import importlib

    for module_name in _CHECK_MODULES:
        module = importlib.import_module(
            f"governance_rule.execution.audit.{module_name}"
        )
        for name, member in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("check_") and name not in names:
                names.append(name)
    return names


def _path_join_literal(node: ast.AST) -> str | None:
    """``root / "a" / "b"`` → ``"a/b"``; anything else → None."""
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (
            isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
        ):
            return None
        parts.append(node.right.value)
        node = node.left
    if isinstance(node, ast.Name) and node.id == "root" and parts:
        return "/".join(reversed(parts))
    return None


def _is_missing_guard(stmt: ast.stmt, var: str) -> bool:
    """``if not <var>.is_file(): errors.append(...); return`` guard."""
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    if not (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Call)
        and isinstance(test.operand.func, ast.Attribute)
        and test.operand.func.attr == "is_file"
        and isinstance(test.operand.func.value, ast.Name)
        and test.operand.func.value.id == var
    ):
        return False
    return bool(stmt.body) and all(
        isinstance(s, (ast.Expr, ast.Return)) for s in stmt.body
    ) and any(isinstance(s, ast.Return) for s in stmt.body)


def _text_read_target(stmt: ast.stmt, var: str) -> str | None:
    """``text = read_text_cached(<var>)`` / ``text = <var>.read_text(...)``
    → assigned name; else None."""
    if not (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Call)
    ):
        return None
    func = stmt.value.func
    if (
        isinstance(func, ast.Name)
        and func.id == "read_text_cached"
        and len(stmt.value.args) == 1
        and isinstance(stmt.value.args[0], ast.Name)
        and stmt.value.args[0].id == var
    ):
        return stmt.targets[0].id
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "read_text"
        and isinstance(func.value, ast.Name)
        and func.value.id == var
    ):
        return stmt.targets[0].id
    return None


def _marker_loop(stmt: ast.stmt, text_var: str) -> list[str] | None:
    """``for m in ("a", "b"): if m not in <text_var>: errors.append(...)``
    → marker list; else None."""
    if not (
        isinstance(stmt, ast.For)
        and not stmt.orelse
        and isinstance(stmt.target, ast.Name)
        and isinstance(stmt.iter, (ast.Tuple, ast.List))
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], ast.If)
        and not stmt.body[0].orelse
    ):
        return None
    markers: list[str] = []
    for element in stmt.iter.elts:
        if not (
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
        ):
            return None
        markers.append(element.value)
    marker_var = stmt.target.id
    condition = stmt.body[0].test
    if not (
        isinstance(condition, ast.Compare)
        and isinstance(condition.left, ast.Name)
        and condition.left.id == marker_var
        and len(condition.ops) == 1
        and isinstance(condition.ops[0], ast.NotIn)
        and len(condition.comparators) == 1
        and isinstance(condition.comparators[0], ast.Name)
        and condition.comparators[0].id == text_var
    ):
        return None
    if not all(
        isinstance(s, ast.Expr)
        and isinstance(s.value, ast.Call)
        and isinstance(s.value.func, ast.Attribute)
        and s.value.func.attr == "append"
        for s in stmt.body[0].body
    ):
        return None
    return markers


def _reduce_marker_check(
    function: ast.FunctionDef,
) -> tuple[str, list[str]] | None:
    """Reduce the canonical ``file exists + required markers`` check body
    to ``(relative_path, markers)``; None when the body carries any other
    semantics (stays delegated — never silently weaken a check)."""
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
        body[0].value, ast.Constant
    ):
        body = body[1:]
    if not 2 <= len(body) <= 4:
        return None
    assign = body[0]
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and isinstance(assign.targets[0], ast.Name)
    ):
        return None
    path_var = assign.targets[0].id
    relative = _path_join_literal(assign.value)
    if relative is None:
        return None
    index = 1
    if index < len(body) and isinstance(body[index], ast.If):
        if not _is_missing_guard(body[index], path_var):
            return None
        index += 1
    if index >= len(body):
        return None
    text_var = _text_read_target(body[index], path_var)
    if text_var is None:
        return None
    index += 1
    if index != len(body) - 1:
        return None
    markers = _marker_loop(body[index], text_var)
    if markers is None:
        return None
    return relative, markers


def _iter_reducible_marker_checks() -> dict[str, tuple[str, list[str]]]:
    """``{check_name: (relative_path, markers)}`` for every ``check_*``
    whose body is exactly the canonical markers pattern."""
    import importlib

    reducible: dict[str, tuple[str, list[str]]] = {}
    for module_name in _CHECK_MODULES:
        module = importlib.import_module(
            f"governance_rule.execution.audit.{module_name}"
        )
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        tree = ast.parse(
            Path(module_file).read_text(encoding="utf-8")
        )
        for node in tree.body:
            if not (
                isinstance(node, ast.FunctionDef)
                and node.name.startswith("check_")
            ):
                continue
            reduced = _reduce_marker_check(node)
            if reduced is not None and node.name not in reducible:
                reducible[node.name] = reduced
    return reducible


def _protected_sources(root: Path) -> list[str]:
    """Live protected-source list from the governed snapshots."""
    sys.path.insert(0, str(root))
    from governance_rule.permission_directory.directory_authority import (
        directory_authority_snapshot,
    )
    from governance_rule.governance_policy import governance_policy_snapshot

    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    seen: list[str] = []
    for relative in (
        *policy.authority_files,
        *directory.managed_read_only_registry_paths,
    ):
        if relative not in seen:
            seen.append(relative)
    return seen


def _forbidden_legacy() -> list[str]:
    from governance_rule.execution.audit.audit_protected import (
        FORBIDDEN_LEGACY_SOURCES,
    )
    return [str(p) for p in FORBIDDEN_LEGACY_SOURCES]


def build_manifest(root: Path) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    # --- forbidden legacy paths (native: file-not-exists) -------------
    for relative in _forbidden_legacy():
        checks.append({
            "id": f"forbidden-legacy:{relative}",
            "kind": "file-not-exists",
            "path": relative,
        })

    # --- protected governance sources (native: exists + readonly) -----
    for relative in _protected_sources(root):
        checks.append({
            "id": f"protected-source:{relative}",
            "kind": "file-exists",
            "path": relative,
        })
        checks.append({
            "id": f"protected-source-readonly:{relative}",
            "kind": "file-readonly",
            "path": relative,
        })

    # --- codex / architecture text pollution (native scan) ------------
    codex_root = root / "governance_rule" / "codex"
    if codex_root.is_dir():
        for path in sorted(codex_root.glob("architecture-*.md")):
            checks.append({
                "id": f"architecture-pollution:{path.name}",
                "kind": "text-no-pollution",
                "path": path.relative_to(root).as_posix(),
            })
        for path in sorted(codex_root.glob("*.zh-TW.part-*.txt")):
            checks.append({
                "id": f"mirror-part-pollution:{path.name}",
                "kind": "text-no-pollution",
                "path": path.relative_to(root).as_posix(),
            })

    # --- governed JSON artifacts parse (native json-parses) -----------
    contracts = (
        "main-system/config/ai-connection-contract.json",
        "main-system/config/backend-lifecycle-contract.json",
        "main-system/config/cleanup-caps-policy.json",
        "main-system/config/data-architecture-contract.json",
        "main-system/config/ipc-contract.json",
        "main-system/config/ipc-surface-backend.json",
        "main-system/config/ipc-surface-frontend.json",
        "main-system/config/resident-core.json",
        "main-system/config/sleep-policy.json",
        "main-system/config/sql-schema-contract.json",
        "main-system/config/tool-isolation-policy.json",
        "main-system/config/tool-runtime-contract.json",
        "main-system/config/automation-flows.json",
    )
    for relative in contracts:
        checks.append({
            "id": f"contract-parse:{relative}",
            "kind": "json-parses",
            "path": relative,
        })

    # --- reducible marker checks (native: file-contains) --------------
    # Every ``check_*`` whose body is exactly "file exists + required
    # markers" is emitted as a native file-contains check — the engine's
    # unreadable→FAIL covers the is_file guard.  Bodies carrying any other
    # semantics stay delegated.
    reducible = _iter_reducible_marker_checks()
    for name, (relative, markers) in reducible.items():
        checks.append({
            "id": f"module-markers:{name}",
            "kind": "file-contains",
            "path": relative,
            "markers": markers,
        })

    # --- static contract / structure checks (native reducible) ---------
    def contains(check_id: str, path: str, markers: list[str],
                 optional: bool = False) -> None:
        entry: dict[str, object] = {
            "id": check_id, "kind": "file-contains",
            "path": path, "markers": markers,
        }
        if optional:
            entry["optional"] = True
        checks.append(entry)

    def not_contains(check_id: str, path: str, markers: list[str],
                     optional: bool = False) -> None:
        entry = {
            "id": check_id, "kind": "file-not-contains",
            "path": path, "markers": markers,
        }
        if optional:
            entry["optional"] = True
        checks.append(entry)

    # check_metadata_contract
    contains(
        "metadata-contract:fields",
        "shared-layer/src/shared_layer/metadata_contract.py",
        ["FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
         "FIELD_STATUS", "ResourceMetadata", "validate_qdrant_payload"],
    )
    checks.append({
        "id": "metadata-contract:ownership-doc",
        "kind": "file-exists",
        "path": "shared-layer/docs/DATA_OWNERSHIP_CONTRACT.md",
    })

    # check_shared_layer_structure (dirs physical, sources readonly)
    sys.path.insert(0, str(root))
    from governance_rule.governance_policy import governance_policy_snapshot
    shared = governance_policy_snapshot().shared_layer
    for relative in (shared.module_root, shared.source_root,
                     shared.data_root):
        checks.append({
            "id": f"shared-layer-dir:{relative}",
            "kind": "dir-exists",
            "path": relative,
        })
    for name in ("__init__.py", "channel.py", "store.py"):
        rel = f"{shared.source_root}/shared_layer/{name}"
        checks.append({
            "id": f"shared-layer-source:{name}",
            "kind": "file-exists", "path": rel,
        })
        checks.append({
            "id": f"shared-layer-source-readonly:{name}",
            "kind": "file-readonly", "path": rel,
        })

    # check_sqlite_template
    contains(
        "sqlite-template:tables",
        "shared-layer/sql/sqlite_module_template.sql",
        ["schema_version", "module_metadata", "resource_metadata",
         "audit_event", "reconcile_state",
         "authority_class", "executor_id", "correlation_id"],
    )

    # check_embedded_browser (conditional file scans + required modules)
    for path in (
        "Standalone tools/ai-collaboration/src/backend/services/"
        "ai_collaboration/integration/browser_automation.py",
        "Standalone tools/ai-collaboration/src/backend/services/"
        "ai_collaboration/integration/provider_session.py",
        "Standalone tools/vaultly/src/backend/services/"
        "vaultly/integration/browser_session.py",
    ):
        not_contains(f"embedded-browser:no-playwright:{path}",
                     path, ["from playwright", "import playwright"],
                     optional=True)
    for path in (
        "main-system/requirements.txt",
        "main-system/pyproject.toml",
    ):
        checks.append({
            "id": f"embedded-browser:no-dep:{path}",
            "kind": "file-not-contains",
            "path": path,
            "markers": ["playwright"],
            "optional": True,
            "ignore_case": True,   # Python 側比對前 .lower()
        })
    checks.append({
        "id": "embedded-browser:module",
        "kind": "file-exists",
        "path": "main-system/src-ui/main/embedded-browser.ts",
    })
    checks.append({
        "id": "embedded-browser:client",
        "kind": "file-exists",
        "path": "shared-layer/src/shared_layer/embedded_browser_client.py",
    })

    # check_orphan_scanner
    contains("orphan-scanner:module",
             "shared-layer/src/shared_layer/database/orphan_scanner.py",
             ["scan_orphans"])

    # check_git_tiers (classify()==3 semantic stays delegated)
    contains("git-tiers:module",
             "governance_rule/execution/git_tiers/__init__.py",
             ["TIER1_OPS", "TIER2_OPS", "TIER3_OPS",
              "def classify", "def enforce", "def audit_log"])
    contains("git-tiers:gate-wrapper",
             "scripts/git-gate.py",
             ["from governance_rule.execution.git_tiers import"])
    for hook in ("pre-commit", "pre-merge-commit", "pre-push"):
        checks.append({
            "id": f"git-tiers:hook:{hook}",
            "kind": "file-exists",
            "path": f"governance_rule/git-hooks/{hook}",
        })
    contains("git-tiers:pre-push",
             "governance_rule/git-hooks/pre-push",
             ["GOVERNANCE_AUTHORITY_APPROVAL", "merge-base", "refs/tags/"])

    # check_release_manifest_file (json + required keys)
    checks.append({
        "id": "release-manifest:keys",
        "kind": "json-has-keys",
        "path": "shared-layer/database-release.json",
        "markers": ["release_id", "schema_version", "migration_head",
                    "rls_version", "role_version",
                    "sqlite_template_version",
                    "reconcile_contract_version",
                    "qdrant_contract_version", "query_contract_version",
                    "minimum_runtime_version", "compatibility_range",
                    "state"],
    })

    # check_release_manifest_module — 已由 module-markers 自動歸約覆蓋。

    # check_architecture_sources
    contains("architecture:shared-database-canonical",
             "shared-layer/src/shared_layer/database/__init__.py",
             ["POSTGRESQL_CANONICAL: bool = True"])
    contains("architecture:local-vector-degraded",
             "shared-layer/src/shared_layer/local/vector_store.py",
             ['"engine": "local-vector-degraded-cache"',
              '"canonical": False'])
    for name in ("market_data.py", "xingcheng_tools/search/searxng.py"):
        path = ("Standalone tools/local-model/src/backend/services/"
                f"xingcheng/infrastructure/{name}")
        contains(f"architecture:network-allowlist:{name}",
                 path, ["NETWORK_DESTINATION_ALLOWLIST"])

    # check_main_system_source
    not_contains("main-system:no-legacy-enforcer",
                 "main-system/src-core/main.py",
                 ["GovernanceEnforcer", "governance.enforcer"])
    contains("main-system:launcher-attested",
             "main-system/src-core/main.py",
             ["MainSystemGovernance.from_environment"])
    not_contains("main-system:no-persistent-logger",
                 "main-system/src-core/main.py", ["CoreLogger"])
    contains("main-system:governance-denied",
             "main-system/src-core/core_system/governance_runtime.py",
             ['if tool_id == "governance_rule"'])

    # --- delegated: every Python check not natively covered -----------
    covered = _NATIVE_COVERED | set(reducible)
    delegated_names = [
        name for name in _iter_python_check_names(None)
        if name not in covered
    ]
    for name in delegated_names:
        checks.append({
            "id": f"python-check:{name}",
            "kind": "delegated",
            "reason": "python oracle (transition)",
        })
    # Partial-coverage honesty: the non-reducible halves of covered checks.
    checks.append({
        "id": "python-check:protected-source-semantic",
        "kind": "delegated",
        "reason": "ast.parse / sqlite quick_check / duplicate detection",
    })
    checks.append({
        "id": "python-check:codex-consistency-semantic",
        "kind": "delegated",
        "reason": "codex repository + mirror identity sync",
    })
    checks.append({
        "id": "python-check:git-tiers-classify-failclosed",
        "kind": "delegated",
        "reason": "classify(unknown)==3 runtime semantic",
    })
    checks.append({
        "id": "python-check:embedded-browser-async-playwright",
        "kind": "delegated",
        "reason": "async_playwright ∧ ¬InProcessEmbeddedBrowser 複合條件",
    })

    return {
        "schema": "star-audit-manifest/v1",
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": "governance_rule.execution.audit.export_audit_manifest",
        "coverage": {
            "native_checks": sum(
                1 for c in checks if c["kind"] != "delegated"),
            "delegated_checks": sum(
                1 for c in checks if c["kind"] == "delegated"),
            "native_kinds": [
                "file-exists", "file-not-exists", "file-readonly",
                "dir-exists", "file-contains", "file-not-contains",
                "text-no-pollution", "json-parses", "json-has-keys",
                "glob-min-count",
            ],
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--out",
        default=str(
            Path("governance_rule") / "execution" / "audit"
            / "audit_checks_manifest.json"
        ),
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    manifest = build_manifest(root)
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    coverage = manifest["coverage"]
    print(
        f"manifest written: {out} "
        f"(native={coverage['native_checks']} "
        f"delegated={coverage['delegated_checks']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
