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
  ``file-contains`` / ``file-not-contains`` / ``text-no-pollution`` /
  ``json-parses`` / ``json-has-keys`` / ``json-key-value`` /
  ``glob-min-count`` / ``glob-contains`` / ``glob-not-contains`` /
  ``glob-absent``) — the engine verifies them directly;
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
    "audit_sql_patterns",
    "audit_optimization",
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
    "check_embedded_browser",
    "check_orphan_scanner",
    "check_git_tiers",              # classify()==3 semantic stays delegated
    "check_release_manifest_file",
    "check_release_manifest_module",
    "check_architecture_sources",
    "check_main_system_source",
    "check_reconcile_modules",      # state-store contains + forbidden + owner
    # Audit-suite convergence: fully reducible to native kinds.
    "check_bootstrap_native_entry",    # csproj/Program.cs exists + marker
    "check_channel_gateway_csharp",    # projects + port invariants
    "check_tool_host_native_boundary", # dirs + glob-not-contains + ops
    "check_tool_manifests",            # per-file json assertions (parity delegated)
    "check_typescript_retirement",     # glob-absent (*.ts/*.tsx/*.d.ts)
    "check_tool_isolation_hardening",  # contains + glob-contains union
    "check_third_party_inventory",     # json-key-value typed assertions
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
        markers = _single_marker(body[index], text_var)
    if markers is None:
        return None
    return relative, markers


def _single_marker(stmt: ast.stmt, text_var: str) -> list[str] | None:
    """``if "literal" not in <text_var>: errors.append(...)``
    → single-marker list; else None."""
    if not (isinstance(stmt, ast.If) and not stmt.orelse):
        return None
    condition = stmt.test
    if not (
        isinstance(condition, ast.Compare)
        and isinstance(condition.left, ast.Constant)
        and isinstance(condition.left.value, str)
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
        for s in stmt.body
    ):
        return None
    return [condition.left.value]


def _reduce_dir_filelist_check(
    function: ast.FunctionDef,
) -> tuple[str, list[str]] | None:
    """Reduce the canonical ``dir = root/"a"; for name in ("x.sql",...):
    if not (dir/name).is_file(): errors.append(...)`` body to
    ``(dir_relative, [names])``; else None."""
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
        body[0].value, ast.Constant
    ):
        body = body[1:]
    if len(body) != 2:
        return None
    assign, loop = body
    if not (
        isinstance(assign, ast.Assign)
        and len(assign.targets) == 1
        and isinstance(assign.targets[0], ast.Name)
    ):
        return None
    dir_var = assign.targets[0].id
    dir_relative = _path_join_literal(assign.value)
    if dir_relative is None:
        return None
    if not (
        isinstance(loop, ast.For)
        and not loop.orelse
        and isinstance(loop.target, ast.Name)
        and isinstance(loop.iter, (ast.Tuple, ast.List))
        and len(loop.body) == 1
        and isinstance(loop.body[0], ast.If)
        and not loop.body[0].orelse
    ):
        return None
    names: list[str] = []
    for element in loop.iter.elts:
        if not (
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
        ):
            return None
        names.append(element.value)
    name_var = loop.target.id
    test = loop.body[0].test
    if not (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Call)
        and isinstance(test.operand.func, ast.Attribute)
        and test.operand.func.attr == "is_file"
        and isinstance(test.operand.func.value, ast.BinOp)
        and isinstance(test.operand.func.value.op, ast.Div)
        and isinstance(test.operand.func.value.left, ast.Name)
        and test.operand.func.value.left.id == dir_var
        and isinstance(test.operand.func.value.right, ast.Name)
        and test.operand.func.value.right.id == name_var
    ):
        return None
    if not all(
        isinstance(s, ast.Expr)
        and isinstance(s.value, ast.Call)
        and isinstance(s.value.func, ast.Attribute)
        and s.value.func.attr == "append"
        for s in loop.body[0].body
    ):
        return None
    return dir_relative, names


def _iter_reducible_filelist_checks() -> dict[str, tuple[str, list[str]]]:
    """``{check_name: (dir_relative, names)}`` for canonical
    directory-filelist existence checks."""
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
            reduced = _reduce_dir_filelist_check(node)
            if reduced is not None and node.name not in reducible:
                reducible[node.name] = reduced
    return reducible


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
    # Codex amendment codex-readonly-minimization: the read-only attribute
    # applies to the generated zh-TW mirror parts only; all other
    # protected sources keep existence/integrity checks only.
    for relative in _protected_sources(root):
        checks.append({
            "id": f"protected-source:{relative}",
            "kind": "file-exists",
            "path": relative,
        })
        if relative.startswith(
            "governance_rule/codex/governance_codex.zh-TW.part-"
        ):
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

    # --- reducible directory-filelist checks (native: file-exists) ------
    # ``for name in (literal tuple): if not (dir/name).is_file(): fail``
    # → one file-exists per literal name (e.g. check_sql_migrations).
    filelist = _iter_reducible_filelist_checks()
    for name, (dir_relative, names) in filelist.items():
        for filename in names:
            checks.append({
                "id": f"dir-filelist:{name}:{filename}",
                "kind": "file-exists",
                "path": f"{dir_relative}/{filename}",
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
         "FIELD_STATUS", "ResourceMetadata", "validate_vector_payload"],
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
        # async_playwright 僅在 InProcessEmbeddedBrowser 持有者檔內合法
        # （oracle: ``async_playwright ∧ ¬InProcessEmbeddedBrowser`` → 錯）
        checks.append({
            "id": f"embedded-browser:no-async-playwright:{path}",
            "kind": "file-not-contains-unless",
            "path": path,
            "markers": ["async_playwright"],
            "unless": ["InProcessEmbeddedBrowser"],
            "optional": True,
        })
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
        "path": "main-system/src-tauri/src/webview_host/mod.rs",
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
                    "reconcile_contract_version",
                    "query_contract_version",
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
    for name in ("market_data.py", "xingcheng_tools/search/searchd.py"):
        path = ("Standalone tools/local-model/src/backend/services/"
                f"xingcheng/infrastructure/{name}")
        contains(f"architecture:network-allowlist:{name}",
                 path, ["NETWORK_DESTINATION_ALLOWLIST"])

    # check_reconcile_modules — two-file contains/forbidden semantics.
    # shared_layer/reconcile.py: must hold ReconcileStateStore and must
    # NOT hold decision surfaces; core_system/data_reconciliation.py is
    # the sole decision owner (must hold class ReconcileService).
    contains("reconcile:state-store",
             "shared-layer/src/shared_layer/reconcile.py",
             ["ReconcileStateStore"])
    not_contains("reconcile:no-decision-surface",
                 "shared-layer/src/shared_layer/reconcile.py",
                 ["class ReconcileService",
                  "def _push_to_central",
                  "def _pull_from_central"])
    contains("reconcile:decision-owner",
             "main-system/src-core/core_system/data_reconciliation.py",
             ["class ReconcileService"])

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

    # check_bootstrap_native_entry — csproj + Program.cs + contract marker.
    _bootstrap_dir = "main-system/launcher/src/GPTBridge.Bootstrap"
    checks.append({
        "id": "bootstrap-entry:csproj", "kind": "file-exists",
        "path": f"{_bootstrap_dir}/GPTBridge.Bootstrap.csproj",
    })
    checks.append({
        "id": "bootstrap-entry:program", "kind": "file-exists",
        "path": f"{_bootstrap_dir}/Program.cs",
    })
    contains("bootstrap-entry:contract-marker",
             f"{_bootstrap_dir}/Program.cs", ["--prepare-only"])

    # check_channel_gateway_csharp — library + test project + invariants.
    _channel_lib = (
        "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels")
    for rel in (
        f"{_channel_lib}/GPTBridge.Channels.csproj",
        "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels.Tests/"
        "GPTBridge.Channels.Tests.csproj",
        f"{_channel_lib}/A263Channel.cs",
    ):
        checks.append({
            "id": f"channel-gateway:{rel.rsplit('/', 1)[-1]}",
            "kind": "file-exists", "path": rel,
        })
    contains("channel-gateway:port-invariants",
             f"{_channel_lib}/A263Channel.cs",
             ["Stopwatch.GetTimestamp", "ReconnectAsync"])

    # check_tool_host_native_boundary — host/test dirs, forbidden
    # governance primitives over *.cs, proxy ops surface, spawn wiring.
    _tool_host = "shared-layer/csharp/GPTBridge.ToolHost/GPTBridge.ToolHost"
    for rel in (_tool_host, f"{_tool_host}.Tests"):
        checks.append({
            "id": f"tool-host:dir:{rel.rsplit('/', 1)[-1]}",
            "kind": "dir-exists", "path": rel,
        })
    for marker in (
        "HMACSHA", "issue_token", "launcher_key", "integrity_manifest",
        "identity_attestation", "gptbridge_transport", "Npgsql",
        "pg_notify",
    ):
        checks.append({
            "id": f"tool-host:forbidden:{marker}",
            "kind": "glob-not-contains",
            "glob": f"{_tool_host}/*.cs", "markers": [marker],
        })
    checks.append({
        "id": "tool-host:proxy-client", "kind": "file-exists",
        "path": f"{_tool_host}/TransportProxyClient.cs",
    })
    contains("tool-host:proxy-ops",
             f"{_tool_host}/TransportProxyClient.cs",
             ['"hello"', '"claim"', '"respond"', '"request_cancelled"',
              '"notification_stamp"'])
    contains("tool-host:spawn-exe-branch",
             "main-system/src-core/tasks/toolbox_start_spawn_process.py",
             ['source_entry.suffix.lower() == ".exe"'])
    contains("tool-host:resolver-native-entry",
             "main-system/src-core/tasks/tool_path_resolver.py",
             ["native_entry"])

    # check_typescript_retirement (A348) — no authored .ts/.tsx/.d.ts
    # outside the noise/exclusion set; dotdirs skipped implicitly.
    _ts_exclude = [
        "venv", "node_modules", "__pycache__", "dist", "dist-ui",
        "build", "release", "releases", "runtime", "out",
    ]
    for pattern in ("*.ts", "*.tsx", "*.d.ts"):
        checks.append({
            "id": f"typescript-retirement:{pattern}",
            "kind": "glob-absent", "path": "", "glob": pattern,
            "exclude": _ts_exclude,
        })

    # check_gpu_coordinator_lazy_torch — 部分歸約：lazy probe 與
    # nvidia-smi/torch 兩探針的 marker 存在性原生檢查；AST 頂層
    # import 判定與 query_gpu 內部呼叫順序語義留 delegated。
    contains("gpu-coordinator:lazy-probe-markers",
             "shared-layer/src/shared_layer/adaptive/gpu_coordinator.py",
             ["def _torch()", "_query_via_nvidia_smi",
              "_query_via_torch"])

    # check_jax_sft_retrace_bound — 部分歸約：fused step / bucketed
    # collation / traced-lr 簽名的 literal markers 原生檢查；
    # _COLLATE_BUCKET 數值 regex 與 donate_argnums 鄰近視窗語義留
    # delegated。
    _sft = (
        "Standalone tools/local-model/src/backend/services/xingcheng/"
        "infrastructure/native_transformer/jax_backend/sft.py")
    contains("jax-sft:fused-step-markers", _sft,
             ["_COLLATE_BUCKET", "train_step = jax.jit(",
              "donate_argnums",
              "def _train_step(params, opt_state, input_ids, labels, lr)",
              "eval_loss = jax.jit(", "collate_bucket",
              "def _choose_bucket"])

    # Python test-lane retirement (native-test-runner-register /
    # test-framework-final-ownership) — the forbidden artifacts must
    # stay absent; the migration worklist must exist and parse.
    for forbidden in (
        "governance_rule/execution/"
        "legacy_python_verification_adapter.py",
        "pytest.ini",
        "conftest.py",
        "native/test_suites/proxy_wire_agent.py",
        "scripts/devin-cli-p6-test-sla.py",
    ):
        checks.append({
            "id": f"python-retirement:forbidden:{forbidden}",
            "kind": "file-not-exists", "path": forbidden,
        })
    _retire = (
        "governance_rule/execution/audit/"
        "pytest_retirement_inventory.json")
    checks.append({
        "id": "python-retirement:inventory-parses",
        "kind": "json-parses", "path": _retire,
    })
    checks.append({
        "id": "python-retirement:inventory-keys",
        "kind": "json-has-keys", "path": _retire,
        "markers": ["registry", "status_enum", "delete_gate",
                    "rows", "fixtures", "bounded_consumers"],
    })

    # Python-minimization ratchet — native replacement for the retired
    # pytest gate (test_python_minimization_gates.py, inventory class H).
    # The baseline JSON carries the embedded measurement recipe; the
    # engine scans/classifies/counts and fails on any over-budget bucket.
    checks.append({
        "id": "python-minimization:bucket-budget",
        "kind": "py-bucket-budget",
        "path": "main-system/config/python-minimization-baseline.json",
    })

    # check_tool_isolation_hardening (A266) — isolation controls 與
    # spawn 控制的 marker 檢查；spawn 兩檔 union 語義以 glob-contains
    # 表達（marker 落在任一命中檔即成立）。
    contains("tool-isolation:controls",
             "main-system/src-core/core_system/tool_isolation.py",
             ["_record_isolation_audit", "job_assigned",
              "job-assignment-failed"])
    for marker in ("stdin=subprocess.DEVNULL", "close_fds=True"):
        checks.append({
            "id": f"tool-isolation:spawn:{marker.split('=')[-1]}",
            "kind": "glob-contains",
            "glob": "main-system/src-core/tasks/toolbox_start_spawn*.py",
            "markers": [marker],
        })

    # check_third_party_inventory — inventory JSON 的型別化值檢查；
    # formal=false 以 bool 等值、formality 前綴／等值以字串算子表達，
    # 與 Python ``is not False``/startswith/== 斷言逐一對齊。
    _inventory = (
        "governance_rule/execution/third_party_management/"
        "tool_inventory.json")
    checks.append({
        "id": "third-party-inventory:parse",
        "kind": "json-parses", "path": _inventory,
    })
    checks.append({
        "id": "third-party-inventory:dependency-formality",
        "kind": "json-key-value", "path": _inventory,
        # tools 為 id-keyed 陣列（Python dict-comp keyed by id）；
        # name[KEY] 選取 id==KEY 的元素。
        "markers": [
            "tools[pybind11].formal=false",
            "tools[pybind11].formality^=approved-implementation-",
            "tools[uv].formal=false",
            "tools[uv].formality^=approved-implementation-",
            "tools[local-rag].formal=false",
            "tools[local-rag].formality=bounded-degraded-fallback",
        ],
    })

    # check_bounded_worker_pools — 部分歸約：thread_budget 模組存在與
    # 必要入口 markers 原生；executor 邊界掃描（AST/regex）留 delegated。
    contains("worker-pools:thread-budget-module",
             "shared-layer/src/shared_layer/performance/thread_budget.py",
             ["CORE_BUDGET_CAP = 5", "bounded_workers",
              "bounded_threads", "allocation_within_budget"])

    # check_contract_axes — 部分歸約：各軸 contract_version>=1 與
    # minimum_supported>=0 原生；min<=version 跨鍵比較、schema
    # fallback 解析與非整數檢查留 delegated。Markers 依匯出當下檔案
    # 內容產生——鍵不存在時 Python 側本就略過，不憑空加嚴。
    for axis in ("ai-connection", "backend-lifecycle",
                 "data-architecture", "ipc", "sql-schema",
                 "tool-runtime"):
        path = f"main-system/config/{axis}-contract.json"
        markers: list[str] = []
        try:
            axis_data = json.loads(
                (root / path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 交給引擎 unreadable FAIL
            axis_data = None
        if isinstance(axis_data, dict):
            if axis_data.get("contract_version") is not None:
                markers.append("contract_version>=1")
            if axis_data.get(
                    "minimum_supported_contract_version") is not None:
                markers.append(
                    "minimum_supported_contract_version>=0")
            schema = axis_data.get("schema")
            if axis_data.get("contract_version") is None and isinstance(
                    schema, str) and "/v" in schema:
                markers.append(
                    f"schema^={schema.rsplit('/v', 1)[0]}/v")
        if markers:
            checks.append({
                "id": f"contract-axis:version:{axis}",
                "kind": "json-key-value", "path": path,
                "markers": markers,
            })

    # check_tool_manifests — 部分歸約：逐 manifest 檔案層級斷言原生。
    # 列舉規則對齊 oracle 的四層掃描（depth-1/2 頂層、depth-3/4 嵌套）；
    # manifest 為匯出期讀取的資料來源——跨檔 identity parity、label
    # regex、retired status casefold 等語義半部仍走 delegated row。
    from governance_rule.code_rule_directory import (
        code_rule_directory_snapshot,
    )
    _required_locale_keys = sorted(
        code_rule_directory_snapshot().required_locale_keys)
    _manifest_artifact_roots = frozenset({"worktrees", "backups"})

    def _manifest_scanned(p: Path) -> bool:
        parts = p.relative_to(root).parts
        if parts[:3] in (
            ("main-system", "runtime", "releases"),
            ("main-system", "runtime", "temp"),
        ):
            return False
        return (not parts[0].startswith(".")
                and parts[0] not in _manifest_artifact_roots)

    def _emit_manifest(manifest_path: Path, *, top_level: bool,
                       expected_owner: str | None) -> None:
        rel = manifest_path.relative_to(root).as_posix()
        checks.append({"id": f"tool-manifest:parse:{rel}",
                       "kind": "json-parses", "path": rel})
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(manifest, dict):
            return
        lifecycle = manifest.get("lifecycle")
        retired = (
            isinstance(lifecycle, dict)
            and str(lifecycle.get("status") or "").strip().casefold()
            == "retired")
        tool_id = str(manifest.get("id") or "")
        if retired:
            checks.append({
                "id": f"tool-manifest:retired:{rel}",
                "kind": "json-key-value", "path": rel,
                "markers": [
                    "enabled=false",
                    "lifecycle.stoppable=false",
                    "status!=running",
                    "main_system_independent_tool!=true",
                ],
            })
            return
        markers = ["name_key=tool.name"]
        absent = ["name"]
        if isinstance(manifest.get("window"), dict):
            markers.append("window.title_key=tool.window_title")
            absent.append("window.title")
        if top_level:
            code_scope = (
                "project-source-excluding-governance-rule"
                if tool_id == "xingcheng" else "tool-root-only")
            db_scope = (
                "opaque-central-index-read-and-xingcheng-internal"
                "-read-write" if tool_id == "xingcheng"
                else "none" if tool_id == "governance_rule"
                else "tool-database-only")
            markers.append(f"permissions.code_scope={code_scope}")
            markers.append(f"permissions.database_scope={db_scope}")
            if tool_id == "governance_rule":
                markers.extend([
                    "status=running",
                    "lifecycle.startup=default-before-main-system",
                    "lifecycle.directLoad=true",
                    "lifecycle.encapsulated=false",
                    "lifecycle.optional=false",
                    "lifecycle.stoppable=false",
                    "lifecycle.disableable=false",
                    "lifecycle.unloadable=false",
                ])
                absent.append("executable")
        else:
            markers.append(f"physical_owner_root={expected_owner}")
        checks.append({
            "id": f"tool-manifest:values:{rel}",
            "kind": "json-key-value", "path": rel,
            "markers": markers,
        })
        checks.append({
            "id": f"tool-manifest:absent:{rel}",
            "kind": "json-key-absent", "path": rel,
            "markers": absent,
        })
        checks.append({
            "id": f"tool-manifest:dicts:{rel}",
            "kind": "json-has-keys", "path": rel,
            "markers": ["permissions", "capabilities"],
        })
        locale_rel = (
            manifest_path.parent / "locales" / "zh-TW.json"
        ).relative_to(root).as_posix()
        checks.append({
            "id": f"tool-locale:exists:{rel}",
            "kind": "file-exists", "path": locale_rel,
        })
        checks.append({
            "id": f"tool-locale:parse:{rel}",
            "kind": "json-parses", "path": locale_rel,
        })
        checks.append({
            "id": f"tool-locale:keys:{rel}",
            "kind": "json-has-keys", "path": locale_rel,
            "markers": list(_required_locale_keys),
        })

    _standalone_dir = root / "Standalone tools"
    for manifest_path in sorted(root.glob("*/manifest.json")):
        if _manifest_scanned(manifest_path):
            _emit_manifest(manifest_path, top_level=True,
                           expected_owner=None)
    for manifest_path in sorted(
            _standalone_dir.glob("*/manifest.json")):
        _emit_manifest(manifest_path, top_level=True,
                       expected_owner=None)
    for manifest_path in sorted(
            _standalone_dir.glob("*/*/manifest.json")):
        _emit_manifest(manifest_path, top_level=False,
                       expected_owner=manifest_path.parent.parent.name)
    for manifest_path in sorted(
            root.glob("*/*/*/*/manifest.json")):
        if _manifest_scanned(manifest_path):
            _emit_manifest(
                manifest_path, top_level=False,
                expected_owner=manifest_path.parent.parent.parent.name)

    # --- delegated: every Python check not natively covered -----------

    # --- delegated: every Python check not natively covered -----------
    # Each delegated row carries an explicit ``python`` target so the
    # delegated lane (same-request execution, G96) can resolve it without
    # a second mapping table — unresolvable rows fail closed.
    covered = _NATIVE_COVERED | set(reducible) | set(filelist)
    delegated_names = [
        name for name in _iter_python_check_names(None)
        if name not in covered
    ]
    for name in delegated_names:
        checks.append({
            "id": f"python-check:{name}",
            "kind": "delegated",
            "reason": "python oracle (transition)",
            "python": name,
        })
    # Partial-coverage honesty: the non-reducible halves of covered checks.
    # ``python`` points at the parent check — re-running the full oracle
    # check covers the delegated semantic half (superset, never weaker).
    checks.append({
        "id": "python-check:protected-source-semantic",
        "kind": "delegated",
        "reason": "ast.parse / sqlite quick_check / duplicate detection",
        "python": "check_protected_sources",
    })
    checks.append({
        "id": "python-check:codex-consistency-semantic",
        "kind": "delegated",
        "reason": "codex repository + mirror identity sync",
        "python": "check_codex_consistency",
    })
    checks.append({
        "id": "python-check:git-tiers-classify-failclosed",
        "kind": "delegated",
        "reason": "classify(unknown)==3 runtime semantic",
        "python": "check_git_tiers",
    })
    checks.append({
        "id": "python-check:tool-manifests-semantic",
        "kind": "delegated",
        "reason": "cross-manifest identity parity / label regex / "
                  "capability registry semantics",
        "python": "check_tool_manifests",
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
                "file-not-contains-unless",
                "text-no-pollution", "json-parses", "json-has-keys",
                "json-key-absent",
                "glob-min-count", "glob-not-contains", "glob-absent",
                "glob-contains", "json-key-value", "py-bucket-budget",
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
