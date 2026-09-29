"""AST reducers mapping check_* functions to native manifest kinds (split from export_audit_manifest)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

# Check modules mirrored by audit_checks.audit_runtime_governance.
_CHECK_MODULES = (
    "audit_artifacts",
    "audit_data_governance",
    "audit_retention_archive",
    "audit_integrity",
    "audit_release",
    "audit_release_flow",
    "audit_recovery",
    "audit_recovery_ops",
    "audit_data_layer",
    "audit_runtime_modules",
    "audit_runtime_db_modules",
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
    "check_gpu_coordinator_torch_free",
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


def emit_reducible(ctx) -> None:
    emit = ctx.emit
    checks = ctx.checks
    root = ctx.root
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

    ctx.reducible = reducible
    ctx.filelist = filelist
