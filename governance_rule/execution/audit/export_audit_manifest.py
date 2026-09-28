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
  ``json-key-absent`` / ``json-array-min-count`` /
  ``glob-min-count`` / ``glob-contains`` / ``glob-not-contains`` /
  ``glob-absent`` / ``file-not-contains-unless`` / ``fail``) —
  the engine verifies them directly;
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
    # Audit-path Python retirement: delegated halves resolved natively.
    "check_bounded_worker_pools",      # budget module + per-file max_workers
    "check_codex_text_integrity",      # text-no-pollution on mirror parts
    "check_authority_policy",          # registry literal bindings
    "check_identity_permissions",      # identity/binding/capability markers
    "check_repair_policy",             # repair literal bindings
    "check_shared_layer_policy",       # shared-layer literal bindings
    "check_activation_states",         # mirror row assertions (fail-closed)
    "check_architecture_registry",     # registry json + route bindings
    "check_directory_audit",           # mirror schema/catalog/seal
    "check_directory_schemas",
    "check_directory_catalog_coverage",
    "check_directory_identity_and_format",
    "check_directory_mirror_parity",
    "check_directory_relationships",
    "check_directory_seal",
    "check_provision_classification",
    "check_formal_rules",              # registry rows + evaluator literals
    "check_implementation_obligations",
    "check_tool_identity_registration",
    "check_contract_axes",
    "check_sql_anti_patterns",         # baseline + export-time live diff
    "check_gpu_coordinator_lazy_torch",
    "check_renderer_idle_gating",
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

    def emit(cid: str, kind: str, path: str = "", **kw: object) -> None:
        row: dict[str, object] = {"id": cid, "kind": kind}
        if path:
            row["path"] = path
        row.update(kw)
        checks.append(row)

    # --- forbidden legacy paths (native: file-not-exists) -------------
    for relative in _forbidden_legacy():
        emit(f"forbidden-legacy:{relative}", "file-not-exists", relative)

    # --- protected governance sources (native: exists + readonly) -----
    # Codex amendment codex-readonly-minimization: the read-only attribute
    # applies to the generated zh-TW mirror parts only; all other
    # protected sources keep existence/integrity checks only.
    for relative in _protected_sources(root):
        emit(f"protected-source:{relative}", "file-exists", relative)
        if relative.startswith(
            "governance_rule/codex/governance_codex.zh-TW.part-"
        ):
            emit(f"protected-source-readonly:{relative}",
                 "file-readonly", relative)

    # --- codex / architecture text pollution (native scan) ------------
    codex_root = root / "governance_rule" / "codex"
    if codex_root.is_dir():
        for path in sorted(codex_root.glob("architecture-*.md")):
            emit(f"architecture-pollution:{path.name}",
                 "text-no-pollution", path.relative_to(root).as_posix())
        for path in sorted(codex_root.glob("*.zh-TW.part-*.txt")):
            emit(f"mirror-part-pollution:{path.name}",
                 "text-no-pollution", path.relative_to(root).as_posix())

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
        emit(f"contract-parse:{relative}", "json-parses", relative)

    # --- reducible marker checks (native: file-contains) --------------
    # Every ``check_*`` whose body is exactly "file exists + required
    # markers" is emitted as a native file-contains check — the engine's
    # unreadable→FAIL covers the is_file guard.  Bodies carrying any other
    # semantics stay delegated.
    reducible = _iter_reducible_marker_checks()
    for name, (relative, markers) in reducible.items():
        emit(f"module-markers:{name}", "file-contains", relative,
             markers=markers)

    # --- reducible directory-filelist checks (native: file-exists) ------
    # ``for name in (literal tuple): if not (dir/name).is_file(): fail``
    # → one file-exists per literal name (e.g. check_sql_migrations).
    filelist = _iter_reducible_filelist_checks()
    for name, (dir_relative, names) in filelist.items():
        for filename in names:
            emit(f"dir-filelist:{name}:{filename}", "file-exists",
                 f"{dir_relative}/{filename}")

    # --- static contract / structure checks (native reducible) ---------
    def contains(check_id: str, path: str, markers: list[str],
                 optional: bool = False) -> None:
        emit(check_id, "file-contains", path, markers=markers,
             **({"optional": True} if optional else {}))

    def not_contains(check_id: str, path: str, markers: list[str],
                     optional: bool = False) -> None:
        emit(check_id, "file-not-contains", path, markers=markers,
             **({"optional": True} if optional else {}))

    # check_metadata_contract
    contains(
        "metadata-contract:fields",
        "shared-layer/src/shared_layer/metadata_contract.py",
        ["FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
         "FIELD_STATUS", "ResourceMetadata", "validate_vector_payload"],
    )
    emit("metadata-contract:ownership-doc", "file-exists",
         "shared-layer/docs/DATA_OWNERSHIP_CONTRACT.md")

    # check_shared_layer_structure (dirs physical, sources readonly)
    sys.path.insert(0, str(root))
    from governance_rule.governance_policy import governance_policy_snapshot
    shared = governance_policy_snapshot().shared_layer
    for relative in (shared.module_root, shared.source_root,
                     shared.data_root):
        emit(f"shared-layer-dir:{relative}", "dir-exists", relative)
    for name in ("__init__.py", "channel.py", "store.py"):
        emit(f"shared-layer-source:{name}", "file-exists",
             f"{shared.source_root}/shared_layer/{name}")

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
             ['"engine": "rust-vectord-degraded"',
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

    # check_jax_sft_retrace_bound — RETIRED (B167): JAX/XLA retired with
    # zero source/dependency/artifact role and no transitional period;
    # the former framework=jax verification is retired.

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

    # test_global_cleaner_retired.py (inventory H) — native migration.
    # lifecycle= appears once in identity_groups.py (inside
    # GLOBAL_CLEANER_IDENTITY); dir-absence covers the retired sources.
    contains("global-cleaner-retired:identity",
             "governance_rule/permission_directory/registries/permissions/"
             "identity_groups.py",
             ["GLOBAL_CLEANER_IDENTITY", 'bound_tool_id="global-cleaner"',
              'lifecycle="retired"'])
    checks.append({"id": "global-cleaner-retired:absent",
                   "kind": "file-not-exists",
                   "path": "Standalone tools/global-cleaner"})

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
                       expected_owner: str | None,
                       self_health: bool = False) -> None:
        rel = manifest_path.relative_to(root).as_posix()
        emit(f"tool-manifest:parse:{rel}", "json-parses", rel)
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
            emit(f"tool-manifest:retired:{rel}", "json-key-value", rel,
                 markers=[
                     "enabled=false",
                     "lifecycle.stoppable=false",
                     "status!=running",
                     "main_system_independent_tool!=true",
                 ])
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
        emit(f"tool-manifest:values:{rel}", "json-key-value", rel,
             markers=markers)
        emit(f"tool-manifest:absent:{rel}", "json-key-absent", rel,
             markers=absent)
        emit(f"tool-manifest:dicts:{rel}", "json-has-keys", rel,
             markers=["permissions", "capabilities"])
        locale_rel = (
            manifest_path.parent / "locales" / "zh-TW.json"
        ).relative_to(root).as_posix()
        emit(f"tool-locale:exists:{rel}", "file-exists", locale_rel)
        emit(f"tool-locale:parse:{rel}", "json-parses", locale_rel)
        emit(f"tool-locale:keys:{rel}", "json-has-keys", locale_rel,
             markers=list(_required_locale_keys))
        # self-health 覆蓋面（audit_self_health 三層 glob 掃到的
        # manifest 才走此列；depth-4 嵌套僅 tool-manifests 語義）。
        # retired / enabled=false 的擁有者不承擔覆蓋宣告屏障。
        if not self_health or manifest.get("enabled") is False:
            return
        tool_root = manifest_path.parent.resolve()
        targets = manifest.get("test_targets")
        if not tool_id or not isinstance(targets, list) or not targets:
            emit(f"self-health:test-targets:{rel}", "fail",
                 reason="governed tool must declare test_targets")
            return
        for index, raw_target in enumerate(targets):
            target = str(raw_target or "").strip()
            base = f"self-health:test-target:{tool_id}:{index}"
            if target.startswith("pending-native:"):
                # 對齊 oracle 第一方向（PENDING row 位於宣告前綴之下）；
                # 反方向（宣告巢於 row dir 內）在 tool-root 宣告下不可能。
                prefix = target.split(":", 1)[1].rstrip("/") + "/"
                emit(f"{base}:pending-native", "json-array-min-count",
                     "governance_rule/execution/audit/"
                     "pytest_retirement_inventory.json",
                     items="rows", min_count=1,
                     markers=["status=PENDING", f"source_test^={prefix}"])
                continue
            if target.startswith("native-suite:"):
                emit(f"{base}:native-suite", "file-exists",
                     f"native/test_suites/suite_{target.split(':', 1)[1]}.cpp")
                continue
            candidate = (tool_root / target).resolve()
            try:
                rel_target = candidate.relative_to(root).as_posix()
                candidate.relative_to(tool_root)
            except ValueError:
                emit(f"{base}:escaped", "fail",
                     reason="test target escaped tool root: "
                            f"{tool_id}: {target}")
                continue
            if candidate.suffix.casefold() == ".py":
                emit(f"{base}:py", "fail",
                     reason="non-conforming Python test target "
                            f"(FORBID:pytest): {tool_id}: {target}")
                continue
            emit(f"{base}:exists", "file-exists", rel_target)

    _standalone_dir = root / "Standalone tools"
    for manifest_path in sorted(root.glob("*/manifest.json")):
        if _manifest_scanned(manifest_path):
            _emit_manifest(manifest_path, top_level=True,
                           expected_owner=None, self_health=True)
    for manifest_path in sorted(
            _standalone_dir.glob("*/manifest.json")):
        _emit_manifest(manifest_path, top_level=True,
                       expected_owner=None, self_health=True)
    for manifest_path in sorted(
            _standalone_dir.glob("*/*/manifest.json")):
        _emit_manifest(manifest_path, top_level=False,
                       expected_owner=manifest_path.parent.parent.name,
                       self_health=True)
    for manifest_path in sorted(
            root.glob("*/*/*/*/manifest.json")):
        if _manifest_scanned(manifest_path):
            _emit_manifest(
                manifest_path, top_level=False,
                expected_owner=manifest_path.parent.parent.parent.name)

    # --- codex-mirror table access (native audit conversion) ----------
    def _load_mirror_tables() -> dict[str, tuple[str, list]]:
        tables: dict[str, tuple[str, list]] = {}
        codex_dir = root / "governance_rule" / "codex"
        for part in sorted( codex_dir.glob("governance_codex.zh-TW.part-*.txt")):
            try:
                doc = json.loads(part.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rel = part.relative_to(root).as_posix()
            for name, rows in (doc.get("tables") or {}).items():
                if isinstance(rows, list):
                    tables.setdefault(name, (rel, rows))
        return tables

    _mirror = _load_mirror_tables()

    _seen_present: set[str] = set()

    def _table_absent(table: str) -> None:
        emit(f"codex-table:absent:{table}", "fail", reason=f"codex mirror table missing: {table}")

    def _table_rows_for(table: str) -> list:
        entry = _mirror.get(table)
        return entry[1] if entry else []

    def _table_assert(cid: str, table: str, markers: list[str], count: int = 1) -> None:
        """``>=count`` rows in the mirror table satisfy all markers."""
        entry = _mirror.get(table)
        if entry is None:
            _table_absent(table)
            return
        emit(cid, "json-array-min-count", entry[0], items=f"tables.{table}", markers=markers, min_count=count)

    def _table_present(table: str) -> None:
        if table in _seen_present:
            return
        _seen_present.add(table)
        entry = _mirror.get(table)
        if entry is None:
            _table_absent(table)
        else:
            emit(f"codex-table:present:{table}", "json-array-min-count",
                 entry[0], items=f"tables.{table}", markers=[],
                 min_count=0)

    _MARKER_TOKENS = ("!=", "^=", ">=")

    def _bind(field: str, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, bool):
            return [f"{field}={'true' if value else 'false'}"]
        text = str(value)
        first = min( (text.find(tok) for tok in _MARKER_TOKENS if tok in text), default=-1)
        if first < 0:
            return [f"{field}={text}"]
        return [f"{field}^={text[:first]}"] if first > 0 else []

    _ROW_BIND_CAP = 150

    def _rows_or_count(cid_prefix: str, table: str, idcol: str, extra: "object" = None) -> None:
        rows = _table_rows_for(table)
        if not rows:
            return
        _table_assert(f"{cid_prefix}:count:{table}", table, [], count=len(rows))
        if len(rows) > _ROW_BIND_CAP:
            return
        for row in rows:
            rid = row.get(idcol)
            if rid is None:
                continue
            markers = _bind(idcol, rid)
            if callable(extra):
                markers += extra(row)
            if markers:
                _table_assert(f"{cid_prefix}:{rid}", table, markers)

    def _relativize(path_text: str) -> str:
        norm = path_text.replace("\\", "/").rstrip("/")
        prefix = root.as_posix() + "/"
        if norm.startswith(prefix):
            return norm[len(prefix):]
        return "." if norm == root.as_posix() else norm

    # --- check_contract_axes (G48) ------------------------------------
    for axis in ( "ai-connection", "backend-lifecycle", "data-architecture", "ipc", "sql-schema", "tool-runtime", ):
        rel_axis = f"main-system/config/{axis}-contract.json"
        emit(f"contract-axis:{axis}:exists", "file-exists", rel_axis)
        emit(f"contract-axis:{axis}:parse", "json-parses", rel_axis)
        try:
            _axis_doc = json.loads( (root / rel_axis).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _axis_doc = {}
        _ver = _axis_doc.get("contract_version")
        _min = _axis_doc.get("minimum_supported_contract_version")
        if isinstance(_ver, int):
            bound = _min if isinstance(_min, int) and _min >= 1 else 1
            emit(f"contract-axis:{axis}:version", "json-key-value", rel_axis, markers=[f"contract_version>={bound}"])
        else:
            emit(f"contract-axis:{axis}:schema", "json-has-keys", rel_axis, markers=["schema"])
        if isinstance(_min, int):
            emit(f"contract-axis:{axis}:minimum", "json-key-value",
                 rel_axis,
                 markers=["minimum_supported_contract_version>=0"])

    # --- check_activation_states --------------------------------------
    _arch_roots = {
        str(r.get("architecture_code")): str(r.get("physical_root") or "")
        for r in _table_rows_for("project_architecture_directory")
    }
    _retired_sovereigns = sorted({
        str(r.get("sovereign_id") or r.get("id") or "")
        for r in _table_rows_for("sovereigns")
        if "retired" in str(r.get("rank") or "")
    } - {""})
    _act_entry = _mirror.get("architecture_activation_states")
    if _act_entry is None:
        _table_absent("architecture_activation_states")
    else:
        act_part, act_rows = _act_entry
        for sid in _retired_sovereigns:
            emit(f"activation:retired-owner:{sid}", "file-not-contains",
                 act_part, markers=[f'"verification_owner":"{sid}"'])
        for row in act_rows:
            code = str(row.get("architecture_code") or "?")
            owner = str(row.get("verification_owner") or "")
            if owner:
                _table_assert(
                    f"activation:owner:{code}",
                    "architecture_activation_states",
                    [f"architecture_code={code}"]
                    + [f"verification_owner!={s}" for s in _retired_sovereigns])
            target = str(row.get("target_root") or "")
            if target and row.get("current_state") in ("active", "mandated"):
                if target.startswith("ARCH_CODE:"):
                    resolved = _arch_roots.get( target.removeprefix("ARCH_CODE:"), "")
                else:
                    resolved = target
                if not resolved:
                    emit(f"activation:unresolved-target:{code}", "fail",
                         reason="activation target_root code unregistered: "
                                f"{code}: {target}")
                else:
                    emit(f"activation:target:{code}", "dir-exists", _relativize(resolved))
            legacy = str(row.get("legacy_root") or "")
            if (legacy
                    and row.get("old_root_deletion_state")
                    == "not-applicable"
                    and not legacy.startswith("ARCH_LEGACY_CODE:")):
                emit(f"activation:legacy-gone:{code}", "file-not-exists", _relativize(legacy))

    # --- check_formal_rules -------------------------------------------
    _table_present("formal_rule_registry")
    _evaluators_src = ( "governance_rule/execution/formal_rules/evaluators.py")
    for frule in _table_rows_for("formal_rule_registry"):
        code = str(frule.get("rule_code") or "")
        if not code:
            continue
        status = str(frule.get("status") or "")
        if status in ("withdrawn", "retired"):
            continue
        contains(f"formal-rule:evaluator:{code}", _evaluators_src, [f'"{code}"'])
        pid = str(frule.get("controlling_provision_id") or "")
        _table_assert(
            f"formal-rule:row:{code}", "formal_rule_registry",
            [f"rule_code={code}", f"controlling_provision_id={pid}", f"status={status}"])
        if status == "active":
            _table_assert(
                f"formal-rule:parity:{code}", "formal_rule_registry",
                [f"rule_code={code}", "parity_status=VERIFIED"])

    # --- check_implementation_obligations (A292) ----------------------
    _table_present("implementation_obligations")
    for oblig in _table_rows_for("implementation_obligations"):
        ocode = str(oblig.get("obligation_code") or "")
        if not ocode:
            continue
        _table_assert(
            f"obligation:row:{ocode}", "implementation_obligations",
            [f"obligation_code={ocode}"]
            + _bind("implementation_owner", oblig.get("implementation_owner"))
            + _bind("target_state", oblig.get("target_state"))
            + _bind("acceptance_evidence", oblig.get("acceptance_evidence"))
            + _bind("current_state", oblig.get("current_state")))
    for rcode in (
        "OBL_LAYERED_ARCHITECTURE", "OBL_ARCHITECTURE_CATALOG",
        "OBL_TEST_SUITE_DIRECTORY", "OBL_TOP_LEVEL_PATHS",
        "OBL_ANTI_JAILBREAK", "OBL_SYSTEM_RELIABILITY",
        "OBL_星澄_AUDIT", "OBL_CHANNEL_ANOMALY_ISOLATION",
        "OBL_NATIVE_PROMOTION_RECORD_CHECKER",
        "OBL_LANGUAGE_DEPENDENCY_DAG_GATE",
        "OBL_DIRECTORY_GOVERNANCE_DATA_CLOSURE",
        "OBL_FORMAL_EVALUATOR_V2_PARITY",
    ):
        _table_assert( f"obligation:required:{rcode}", "implementation_obligations", [f"obligation_code={rcode}"])

    # --- authority policy family (registry literal bindings) ----------
    from governance_rule.governance_policy import ( governance_policy_snapshot as _gps, )
    from governance_rule.permission_directory.directory_authority import ( directory_authority_snapshot as _das, )
    from governance_rule.code_rule_directory import ( code_rule_directory_snapshot as _cds, )
    _policy = _gps()
    _directory = _das()
    _code_rules = _cds()
    _policy_src = "governance_rule/governance_policy.py"
    _dir_src = ("governance_rule/permission_directory/" "directory_authority.py")
    _code_src = "governance_rule/code_rule_directory.py"

    contains("authority-policy:policy", _policy_src, [
        f'authority="{_policy.authority}"',
        f'top_level_rule="{_policy.top_level_rule}"',
        f"governance_rule_count={_policy.governance_rule_count}",
        "governance_rule_partitioning=False",
        "subordinate_governance_rule_definition=False",
        f'permission_hierarchy_role="'
        f'{_policy.permission_hierarchy_role}"',
    ])
    contains("authority-policy:directory", _dir_src, [
        'permission_hierarchy_role='
        '"subordinate-read-only-permission-directory"',
        f"current_version={_policy.authority_version}",
    ])
    contains("authority-policy:code-rules", _code_src, [
        '"codex-v1.32010-is-sole-rule-source"',
        f'governing_source="{_policy.governance_rule_sources[0]}"',
        "independent_authority=False",
        "runtime_write_allowed=False",
        f'canonical_project_root="'
        f'{_policy.code_architecture.all_source_code_root}"',
    ])
    _resp = _policy.system_responsibilities
    contains("authority-policy:responsibilities", _policy_src, [
        f'git="{_resp.git}"',
        f'sql="{_resp.sql}"',
        f'vector_rag="{_resp.vector_rag}"',
        f'local_vector_fallback="{_resp.local_vector_fallback}"',
        f'llm="{_resp.llm}"',
        f'separation="{_resp.separation}"',
        f'governed_flow="{_resp.governed_flow}"',
        f'management_owner="{_resp.management_owner}"',
        "llm_inference_as_source_of_truth=False",
    ])

    _shared = _directory.shared_layer_access_policy
    _activation = _policy.activation
    contains("shared-layer-policy:directory", _dir_src, [
        f'module_root="{_shared.module_root}"',
        'jurisdiction="governance-policy-only"',
        "main_system_module_member=False",
        "token_required=True", "database_only=True",
        "source_write=False", "direct_data_write=False",
        "executable_content=False", "direct_process_instruction=False",
        'unchanneled_instruction="PERMISSION_DENIED"',
        f'database_path="{_shared.database_path}"',
        f'ai_database_path="{_shared.ai_database_path}"',
    ])
    contains("shared-layer-policy:activation", _policy_src, [
        "default_active=True",
        f'activation_order="{_activation.activation_order}"',
        "direct_load_required=True",
        f'independent_tool_packaging_exception="'
        f'{_activation.independent_tool_packaging_exception}"',
        "packaged_executable_allowed=False",
        f'execution_access="{_activation.execution_access}"',
        "encapsulation_allowed=False",
        "optional=False", "stop_permission=False",
        "disable_permission=False", "unload_permission=False",
        f'lifetime="{_activation.lifetime}"',
        f'main_system_start_failure="'
        f'{_activation.main_system_start_failure}"',
        'main_system_repair_authority="governance-policy-only"',
        f'main_system_repair_scope="'
        f'{_activation.main_system_repair_scope}"',
        'repair_completion_gate="governance-reverify-before-normal-mode"',
    ])
    contains("shared-layer-policy:directory-lifecycle", _dir_src, [
        "governance_default_active=True",
        f'governance_activation_order="{_activation.activation_order}"',
        "governance_direct_load=True",
        "governance_packaged_executable_allowed=False",
        f'governance_execution_access="{_activation.execution_access}"',
        "governance_encapsulation_allowed=False",
        "governance_stop_permission=False",
        "governance_disable_permission=False",
        "governance_unload_permission=False",
    ])
    contains("shared-layer-policy:labels", _policy_src, [ "aliases_allowed=False", "category_labels_allowed=False", ])
    not_contains("shared-layer-policy:no-categories", _code_src, ["category_labels=True"])

    _repair = _policy.automatic_repair
    contains("repair-policy:policy", _policy_src, [
        "backup_assistance_allowed=True",
        f'backup_owner="{_repair.backup_owner}"',
        "direct_backup_access=False",
        f'backup_request_channel="{_repair.backup_request_channel}"',
        "authorization_per_step=True",
        "authority_restore_from_backup=False",
    ])
    contains("repair-policy:boundaries",
             "governance_rule/permission_directory/registries/"
             "permissions/capability_boundaries.py",
             ["automatic-repair"])

    from governance_rule.permission_directory.registries.permissions.identity_groups import (
        identity_group_snapshot as _igs,
    )
    from governance_rule.permission_directory.registries.permissions.identity_permissions import (
        identity_permission_snapshot as _ips,
    )
    from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
        capability_boundary_snapshot as _cbs,
    )
    _identity_group = _igs()
    _bindings = _ips()
    _cap_bounds, _repair_bounds = _cbs()
    _ig_src = ("governance_rule/permission_directory/registries/" "permissions/identity_groups.py")
    _ip_src = ("governance_rule/permission_directory/registries/" "permissions/identity_permissions.py")
    _cb_src = ("governance_rule/permission_directory/registries/" "permissions/capability_boundaries.py")
    _registry_dir = ( root / "governance_rule" / "permission_directory" / "registries")
    _registry_files = [ p for p in sorted(_registry_dir.rglob("*.py")) if "__pycache__" not in p.parts]

    def _literal_file(literal: str) -> str | None:
        for reg in _registry_files:
            try:
                if literal in reg.read_text( encoding="utf-8", errors="replace"):
                    return reg.relative_to(root).as_posix()
            except OSError:
                continue
        return None

    def _literal_assert(cid: str, literal: str) -> None:
        found = _literal_file(literal)
        if found is None:
            emit(cid, "fail", reason=f"registry literal absent: {literal}")
        else:
            emit(cid, "file-contains", found, markers=[literal])

    for ident in _identity_group.identities:
        _literal_assert(f"identity:actor:{ident.actor}", f'actor="{ident.actor}"')
        _literal_assert(f"identity:code:{ident.actor}", f'identity_code="{ident.identity_code}"')
        if ident.bound_tool_id:
            _literal_assert(f"identity:tool:{ident.actor}", f'"{ident.bound_tool_id}"')
        if ident.lifecycle != "active":
            _literal_assert(f"identity:lifecycle:{ident.actor}", f'lifecycle="{ident.lifecycle}"')
    for binding in _bindings:
        _literal_assert(f"binding:actor:{binding.actor}", f'actor="{binding.actor}"')
        for cap in binding.capabilities:
            _literal_assert(f"binding:cap:{binding.actor}:{cap}", f'"{cap}"')
    for cap_item in _cap_bounds:
        contains(f"capability:{cap_item.capability}", _cb_src, [f'"{cap_item.capability}"'])
    not_contains("identity:no-arbitrary-storage", _ip_src, ['"shared-layer-read-write"'])
    not_contains("capability:no-arbitrary-storage", _cb_src, ['"shared-layer-read-write"'])
    for approved in _code_rules.approved_actor_names:
        contains(f"approved-actor:{approved}", _code_src, [f'"{approved}"'])
    for approved_tool in _code_rules.approved_tool_ids:
        contains(f"approved-tool:{approved_tool}", _code_src, [f'"{approved_tool}"'])

    _NON_INDEPENDENT = frozenset({ "governance_rule", "shared-layer", "star-chat", "xingcheng-assistant"})
    _retired_ids = {
        i.bound_tool_id for i in _identity_group.identities
        if i.lifecycle == "retired"}
    for ident in _identity_group.identities:
        tid = ident.bound_tool_id
        if (tid in ("main-system",) or tid in _NON_INDEPENDENT or tid in _retired_ids):
            continue
        _literal_assert(f"tool-identity:{tid}", f'actor="governance/tool/{tid}"')
        contains(f"tool-approved:{tid}", _code_src, [f'"{tid}"'])

    # --- check_git_tiers classify-failclosed remainder -----------------
    contains("git-tiers:classify-failclosed",
             "governance_rule/execution/git_tiers/__init__.py",
             ["def classify", "TIER1_OPS", "TIER2_OPS", "TIER3_OPS", "return 3"])
    contains("git-tiers:pre-push-gate", "governance_rule/git-hooks/pre-push",
             ["GOVERNANCE_AUTHORITY_APPROVAL", "merge-base", "refs/tags/"])
    for hook in ("pre-commit", "pre-merge-commit", "pre-push"):
        emit(f"git-tiers:hook:{hook}", "file-exists", f"governance_rule/git-hooks/{hook}")

    # --- check_codex_consistency semantic half -------------------------
    _p1 = "governance_rule/codex/governance_codex.zh-TW.part-1.txt"
    # Mirror carries the authoritative version+hash; the release pin is
    # release-cadence and may lag — bind stable fields, not equality.
    emit("codex-consistency:pin-keys", "json-has-keys",
         "shared-layer/release-dependencies.json",
         markers=["governance_references"])
    emit("codex-consistency:pin-values", "json-key-value",
         "shared-layer/release-dependencies.json",
         markers=[
             "governance_references.codex_identity"
             "=governance-codex://official",
             "governance_references.codex_authority"
             "=postgresql://local/gptbridge_codex",
             "governance_references.codex_version^=20"])
    emit("codex-consistency:mirror-keys", "json-has-keys", _p1,
         markers=["codex_version", "assembled_payload_hash", "part_hash", "mirror_id"])
    for required_table in (
        "metadata", "revision_history", "seal_manifest",
        "certification_policy", "version_evolution_rules",
        "provision_identities", "provision_lineage",
    ):
        _table_present(required_table)
    for tbl, key in (("sovereigns", "sovereign_id"),
                     ("principles", "provision_id"),
                     ("articles", "provision_id"),
                     ("edicts", "provision_id")):
        _rows_or_count(f"codex-id:{tbl}", tbl, key)

    # --- check_directory_audit family ----------------------------------
    from governance_rule.execution.audit.audit_directories import (
        DIRECTORY_OWNERS as _DIR_OWNERS,
        DIRECTORY_TABLES as _DIR_TABLES,
        REQUIRED_SCHEMA_TABLES as _REQ_TABLES,
    )
    for table in sorted(_REQ_TABLES):
        _table_present(table)
    _catalog = _table_rows_for("directory_master_catalog")
    _contract_codes = {
        str(r.get("directory_code"))
        for r in _table_rows_for("directory_format_contract")
    }
    _catalog_codes = {
        str(r.get("directory_code")) for r in _catalog}
    for ccode in sorted(_contract_codes | _catalog_codes):
        _table_assert(f"catalog:contract-has:{ccode}", "directory_master_catalog", [f"directory_code={ccode}"])
        _table_assert(f"catalog:catalog-has:{ccode}", "directory_format_contract", [f"directory_code={ccode}"])
    for crow in _catalog:
        ccode = str(crow.get("directory_code") or "")
        expected_owner = (
            "learning-evidence-sync-sub-sovereign"
            if ccode == "DIR_MAINTENANCE_MANUAL"
            else "permission-sovereign")
        _table_assert(
            f"catalog:owner:{ccode}", "directory_master_catalog",
            [f"directory_code={ccode}", f"owner={expected_owner}"])
        physical = str(crow.get("canonical_name") or "").replace( "-", "_")
        if crow.get("implementation_state") == "active" and physical:
            _table_present(physical)
    # Per-directory-row bindings (identity value + owner + versions —
    for table, idcol in _DIR_TABLES.items():
        _dir_rows = _table_rows_for(table)
        for drow in _dir_rows:
            if drow.get(idcol) is None:
                emit(f"directory:identity-missing:{table}", "fail", reason=f"{table} row missing {idcol}")
        _rows_or_count(f"directory:identity:{table}", table, idcol,
                       extra=lambda r, _t=table: _bind( "owner", _DIR_OWNERS[_t]))
    _class_rows = _table_rows_for("provision_law_classification")
    if _class_rows:
        _table_assert( "provision-class:count", "provision_law_classification", [], count=len(_class_rows))
        for law in sorted({ str(r.get("law_code")) for r in _class_rows if r.get("law_code")}):
            _table_assert(f"provision-law:{law}", "law_structure_directory", [f"law_code={law}"])
    # Seal completeness on the current seal row.
    _seals = _table_rows_for("seal_manifest")
    if not _seals:
        emit("directory:seal-empty", "fail", reason="seal manifest is empty")
    else:
        _current_seal = max( _seals, key=lambda r: str(r.get("version") or ""))
        _sv = str(_current_seal.get("version") or "")
        _table_assert(
            f"directory:seal:{_sv}", "seal_manifest",
            [f"version={_sv}"]
            + _bind("certification_state", _current_seal.get("certification_state"))
            + _bind("content_root", _current_seal.get("content_root"))
            + _bind("identity_root", _current_seal.get("identity_root"))
            + _bind("full_root", _current_seal.get("full_root"))
            + _bind("history_head", _current_seal.get("history_head")))

    # --- check_architecture_registry ------------------------------------
    emit("architecture-registry:parse", "json-parses", "governance_rule/execution/audit/architecture_registry.json")
    for rel_mod in ("main-system/governance/sovereigns/__init__.py",):
        emit(f"sovereign-module:{rel_mod}", "file-exists", rel_mod)
    _routes_src = ( "governance_rule/permission_directory/registries/" "permissions/tool_routes.py")
    try:
        _routes_text = (root / _routes_src).read_text( encoding="utf-8", errors="replace")
    except OSError:
        _routes_text = ""
    _route_ids: set[str] = set()
    for marker in ('"tool_id"', "'tool_id'"):
        _idx = 0
        while True:
            _idx = _routes_text.find(marker, _idx)
            if _idx < 0:
                break
            _tail = _routes_text[_idx + len(marker):]
            for quote in ('"', "'"):
                _start = _tail.find(quote)
                if _start < 0:
                    continue
                _end = _tail.find(quote, _start + 1)
                if _end > 0:
                    _cand = _tail[_start + 1:_end].strip()
                    if _cand:
                        _route_ids.add(_cand)
                    break
            _idx += 1
    for rid in sorted(_route_ids):
        emit(f"architecture-registry:route:{rid}", "file-contains",
             "governance_rule/execution/audit/architecture_registry.json",
             markers=[f'"{rid}"'])

    # --- check_gpu_coordinator_lazy_torch ------------------------------
    _gpu_src = ("shared-layer/src/shared_layer/adaptive/" "gpu_coordinator.py")
    contains("gpu-lazy-torch:probe", _gpu_src, ["def _torch()", "_query_via_nvidia_smi", "def query_gpu"])
    not_contains("gpu-lazy-torch:top-import", _gpu_src,
                 ["\nimport torch\n", "\nimport torch ",
                  "\nimport torch,", "\nimport torch.",
                  "\nfrom torch ", "\nfrom torch."])

    # --- check_jax_sft_retrace_bound ------------------------------------
    # RETIRED (B167): jax_backend/sft.py removed with the JAX framework.

    # --- check_renderer_idle_gating --------------------------------------
    _renderer_base = root / "main-system" / "src-ui" / "renderer"
    if _renderer_base.is_dir():
        for _rfile in sorted(_renderer_base.rglob("*")):
            if (_rfile.suffix not in (".js", ".jsx", ".mjs")
                    or not _rfile.is_file()
                    or "node_modules" in _rfile.parts
                    or "dist" in _rfile.parts):
                continue
            try:
                _rtext = _rfile.read_text( encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "setInterval(" in _rtext:
                checks.append({
                    "id": "renderer-idle-gating:"
                          f"{_rfile.relative_to(root).as_posix()}",
                    "kind": "file-not-contains-unless",
                    "path": _rfile.relative_to(root).as_posix(),
                    "markers": ["setInterval("],
                    "unless": ["visibilityState", "idle-ok", "navigator.onLine", "visibilitychange"],
                })

    # --- check_bounded_worker_pools --------------------------------------
    _pools_src = ("shared-layer/src/shared_layer/performance/" "thread_budget.py")
    contains("worker-pools:budget-module", _pools_src,
             ["CORE_BUDGET_CAP = 5", "bounded_workers", "bounded_threads", "allocation_within_budget"])
    for _pool_root in ("main-system/src-core", "main-system/governance",
                       "shared-layer/src", "governance_rule",
                       "Standalone tools"):
        _pbase = root / _pool_root
        if not _pbase.is_dir():
            continue
        for _pfile in sorted(_pbase.rglob("*.py")):
            _parts = set(_pfile.parts)
            if (_parts & {"__pycache__", ".venv", "bin", "build",
                          "dist", "node_modules", "releases",
                          "runtime", "test", "tests"}
                    or _pfile.name.startswith("test_")):
                continue
            try:
                if b"PoolExecutor" not in _pfile.read_bytes():
                    continue
            except OSError:
                continue
            emit(f"worker-pool:{_pfile.relative_to(root).as_posix()}",
                 "file-contains",
                 _pfile.relative_to(root).as_posix(),
                 markers=["max_workers"])

    # --- check_sql_anti_patterns ------------------------------------------
    emit("sql-patterns:baseline-exists", "file-exists", "governance_rule/execution/audit/sql_patterns_baseline.json")
    emit("sql-patterns:baseline-keys", "json-has-keys",
         "governance_rule/execution/audit/sql_patterns_baseline.json",
         markers=["findings"])
    contains("sql-patterns:scanner-machinery",
             "governance_rule/execution/audit/audit_sql_patterns.py",
             ["collect_finding_keys", "baseline_path", "_SELECT_STAR", "_OFFSET"])
    from governance_rule.execution.audit.audit_sql_patterns import ( collect_finding_keys as _collect_findings, )
    for _violation in sorted(set(_collect_findings(root))):
        emit(f"sql-patterns:finding:{_violation}", "fail",
             reason="sql anti-pattern finding at manifest export: "
                    f"{_violation}")

    # --- protected-source-semantic remainder ------------------------------
    _protected = _protected_sources(root)
    if len(_protected) != len(set(_protected)):
        emit("protected-source:duplicates", "fail", reason="protected governance sources contain duplicates")

    # --- tool-manifests-semantic remainder ------------------------------
    import re as _rem

    _tm_ids: set[str] = set()
    _tm_docs: list[tuple[Path, str, dict]] = []
    _tm_top_roots: set[str] = set()
    _tm_paths = [
        *sorted(p for p in root.glob("*/manifest.json") if _manifest_scanned(p)),
        *sorted((root / "Standalone tools").glob("*/manifest.json")),
        *sorted((root / "Standalone tools").glob("*/*/manifest.json")),
        *sorted(p for p in root.glob("*/*/*/*/manifest.json") if _manifest_scanned(p)),
    ]
    for mpath in _tm_paths:
        try:
            mdoc = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(mdoc, dict):
            continue
        mtid = str(mdoc.get("id") or "")
        mlc = mdoc.get("lifecycle")
        if (isinstance(mlc, dict) and str(mlc.get("status") or "").strip().casefold() == "retired"):
            if not mtid:
                emit(f"tm:{mpath.relative_to(root).as_posix()}:no-id",
                     "fail",
                     reason="retired tool manifest lacks an identifier")
            continue
        _tm_ids.add(mtid)
        por = str(mdoc.get("physical_owner_root") or "")
        _tm_docs.append((mpath, mtid, mdoc))
        if len(mpath.relative_to(root).parts) <= 3 and por:
            _tm_top_roots.add(por)
    for mpath, mtid, mdoc in _tm_docs:
        mrel = mpath.relative_to(root).as_posix()
        parts = mpath.relative_to(root).parts
        por = str(mdoc.get("physical_owner_root") or "")
        if len(parts) >= 4:
            if not por or por != parts[1] or por not in _tm_top_roots:
                emit(f"tm:{mtid}:owner-parity", "fail", reason=f"nested physical_owner_root invalid: {mrel}")
        elif mtid != mpath.parent.name and por != mpath.parent.name:
            emit(f"tm:{mtid}:dir-parity", "fail", reason=f"tool identity mismatch: {mrel}")
        emit(f"tm:{mtid}:id", "json-key-value", mrel, markers=[f"id={mtid}"])
        if _rem.fullmatch( _policy.identifier_labels.tool_id_pattern, mtid) is None:
            emit(f"tm:{mtid}:label", "fail", reason=f"tool identifier is not standardized: {mtid}")
        caps = mdoc.get("capabilities")
        if not isinstance(caps, dict):
            emit(f"tm:{mtid}:caps", "fail", reason=f"tool capabilities are missing: {mtid}")
        for cname in caps if isinstance(caps, dict) else ():
            if (cname not in _code_rules.approved_capability_names
                    or _rem.fullmatch( _policy.identifier_labels.capability_pattern, cname) is None):
                emit(f"tm:{mtid}:cap:{cname}", "fail", reason="capability label not standardized: " f"{mtid}:{cname}")
            else:
                contains(f"tm:{mtid}:cap:{cname}", mrel, [f'"{cname}"'])
        try:
            locale = json.loads( (mpath.parent / "locales" / "zh-TW.json") .read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue  # file-exists/json-parses rows fail natively
        if not isinstance(locale, dict) or not all(
                isinstance(key, str)
                and _rem.fullmatch( _policy.identifier_labels.locale_key_pattern, key)
                and isinstance(value, str)
                for key, value in locale.items()):
            emit(f"tm:{mtid}:locale-schema", "fail", reason=f"zh-TW locale schema invalid: {mtid}")
    _tm_retired_identity = {
        i.bound_tool_id for i in _identity_group.identities
        if i.lifecycle == "retired"}
    _tm_sets = {
        "registered": {
            i.bound_tool_id for i in _identity_group.identities
            if i.bound_tool_id != "main-system"
            and i.bound_tool_id not in _NON_INDEPENDENT
            and i.lifecycle != "retired"},
        "manifest": _tm_ids - _NON_INDEPENDENT - _tm_retired_identity,
        "approved": set(_code_rules.approved_tool_ids)
                    - _NON_INDEPENDENT - _tm_retired_identity,
    }
    for left, right in (("registered", "manifest"),
                        ("manifest", "registered"),
                        ("manifest", "approved"),
                        ("approved", "manifest")):
        for tid in sorted(_tm_sets[left] - _tm_sets[right]):
            emit(f"tm-parity:{left}-not-{right}:{tid}", "fail", reason=f"{left} tool id not in {right} set: {tid}")

    # --- oracle-only: source_ownership_errors -------------------------
    from governance_rule.permission_directory.registries.permissions import ( source_ownership as _so)
    for _owner, _srcs in _so.REQUIRED_OWNED_SOURCES.items():
        for _rel in sorted(_srcs):
            emit(f"owned-source:{_rel}", "file-exists", _rel)
    for _rel in sorted(_so.FORBIDDEN_LEGACY_BUSINESS_SOURCES | {
            "Standalone tools/file-sorter/src/cleanup.py",
            "Standalone tools/file-sorter/src/sorter_v2.py",
            "Standalone tools/file-sorter/src/backend/automation_service.py",
            "main-system/src-core/managers/provider_monitor.py",
            "main-system/scripts/smoke/ai_assistant_visual_smoke.py"}):
        emit(f"forbidden-source:{_rel}", "file-not-exists", _rel)
    emit("owned-source:visual-smoke", "file-exists", "Standalone tools/ai-assistant/scripts/visual_smoke.py")
    not_contains("main-system:ipc-symbols", "main-system/src-core/ipc/server.py",
                 ["_investment_watch_result_log_payload", "_INVESTMENT_WATCH_LOG_"])
    for _pkg, _layers in (
            (_so.AI_ASSISTANT_PACKAGE_ROOT, _so.AI_ASSISTANT_REQUIRED_LAYERS),
            (_so.XINGCHENG_PACKAGE_ROOT, _so.XINGCHENG_REQUIRED_LAYERS),
            (_so.AI_COLLABORATION_PACKAGE_ROOT, _so.AI_COLLABORATION_REQUIRED_LAYERS),
            (_so.INVESTMENT_MOBILE_PACKAGE_ROOT, _so.INVESTMENT_MOBILE_REQUIRED_LAYERS),
            (_so.FILE_SORTER_PACKAGE_ROOT, _so.FILE_SORTER_REQUIRED_LAYERS),
            (_so.VAULTLY_PACKAGE_ROOT, _so.VAULTLY_REQUIRED_LAYERS),
            (_so.STAR_CHAT_PACKAGE_ROOT, _so.STAR_CHAT_REQUIRED_LAYERS)):
        for _layer in sorted(_layers):
            emit(f"pkg-layer:{_pkg}:{_layer}", "file-exists", f"{_pkg}/{_layer}/__init__.py")
        for _stray in sorted((root / _pkg).glob("*.py")):
            if _stray.name != "__init__.py":
                emit(f"pkg-stray:{_stray.relative_to(root).as_posix()}", "fail",
                     reason="source outside owned layer")
    _trees = sorted(
        {p.as_posix() for p in root.glob("*/src") if p.is_dir()}
        | {p.as_posix() for p in (root / "Standalone tools").glob("*/src") if p.is_dir()})
    for _prefix, _owner_root in _so.OWNED_IMPORT_PREFIXES.items():
        for _tree in _trees:
            _trel = _relativize(_tree)
            if not _trel.startswith(_owner_root + "/"):
                emit(f"cross-import:{_prefix}:{_trel}", "tree-not-contains",
                     _trel, glob="*.py",
                     markers=[f"import {_prefix}", f"from {_prefix}"])
    emit("shared-layer:forbidden-terms", "tree-not-contains",
         "shared-layer/src", glob="*.py", ignore_case=True,
         markers=sorted(_so.SHARED_LAYER_FORBIDDEN_TERMS))
    emit("main-system:forbidden-business", "tree-not-contains",
         "main-system/src-core", glob="*.py", ignore_case=True,
         markers=sorted(_so.MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS))
    emit("ai-assistant:forbidden-network", "tree-not-contains",
         _so.AI_ASSISTANT_PACKAGE_ROOT, glob="*.py",
         markers=[f"{v} {m}" for v in ("import", "from")
                  for m in ("aiohttp", "httpx", "requests", "smtplib")]
         + ["urllib.request", "urlopen("])
    _shared_src = root / _so.SHARED_LAYER_ROOT
    if _shared_src.is_dir():
        for _sf in sorted(_shared_src.rglob("*.py")):
            _srel = _sf.relative_to(_shared_src).as_posix()
            if (_srel not in _so.SHARED_LAYER_ALLOWED_SOURCES
                    and not _srel.startswith(_so.SHARED_LAYER_ALLOWED_PREFIXES)):
                emit(f"unowned-shared:{_srel}", "fail", reason="unowned shared-layer source")

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
                "json-array-min-count", "fail",
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
